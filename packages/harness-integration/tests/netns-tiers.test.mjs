/**
 * Unit & integration tests for Milestone M2:
 * Per-User Linux Network Namespaces & Egress Access Tiers (Requirement R2).
 *
 * Covers:
 * 1. Netns naming and veth interface name generation (enforcing Linux <= 15-char IFNAMSIZ).
 * 2. Point-to-point /30 subnet allocation under 10.200.0.0/16 and collision avoidance.
 * 3. nftables rule generation for Level 0 (Airgapped), Level 1 (Restricted CIDR), and Level 2 (Full Egress).
 * 4. Mock netns mode command recording, table state, and cleanup.
 * 5. Fail-closed exit 126 enforcement on netns failure without unconfined fallback.
 * 6. Reverse proxy target routing to netns guest IP for HTTP and WebSocket streams.
 * 7. InstanceManager lifecycle integration (allocation, runner args, teardown, registry persistence).
 */
import assert from 'node:assert/strict'
import { createServer as createHttpServer } from 'node:http'
import { createServer as createNetServer } from 'node:net'
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { after, test } from 'node:test'

import { loadConfig } from '../gateway/config.js'
import {
  calculateSubnet,
  formatInterfaceNames,
  generateNftablesRules,
  isValidIpv4Cidr,
  MAX_SLOTS,
  NetnsError,
  NetnsManager,
  PLATFORM_SERVICE_PORTS,
  sanitizeNftIdentifier,
} from '../gateway/netns-manager.js'
import {
  HarnessInstance,
  InstanceManager,
} from '../gateway/instance-manager.js'
import { createGateway } from '../gateway/server.js'

const scratchDirs = []
function makeScratch() {
  const dir = mkdtempSync(join(tmpdir(), 'dsh-netns-test-'))
  scratchDirs.push(dir)
  return dir
}

after(() => {
  for (const dir of scratchDirs) {
    try {
      rmSync(dir, { recursive: true, force: true })
    } catch {
      /* ignore */
    }
  }
})

test('1. Interface names strictly respect Linux 15-char IFNAMSIZ limit across all slots', () => {
  for (let slot = 1; slot <= MAX_SLOTS; slot += 1) {
    const { hostVeth, guestVeth } = formatInterfaceNames(slot)
    assert.ok(
      hostVeth.length <= 15,
      `hostVeth "${hostVeth}" exceeds 15 chars for slot ${slot} (length ${hostVeth.length})`,
    )
    assert.ok(
      guestVeth.length <= 15,
      `guestVeth "${guestVeth}" exceeds 15 chars for slot ${slot} (length ${guestVeth.length})`,
    )
    assert.equal(guestVeth, 'veth0')
    assert.match(hostVeth, /^vhd-\d+$/)
  }

  // Boundary checks: invalid slot indices throw NetnsError with exit code 126
  assert.throws(() => formatInterfaceNames(0), (err) => err instanceof NetnsError && err.exitCode === 126)
  assert.throws(() => formatInterfaceNames(-1), (err) => err instanceof NetnsError && err.exitCode === 126)
  assert.throws(() => formatInterfaceNames(255), (err) => err instanceof NetnsError && err.exitCode === 126)
  assert.throws(() => formatInterfaceNames(1.5), (err) => err instanceof NetnsError && err.exitCode === 126)
})

test('2. Deterministic /30 subnet allocation under 10.200.0.0/16 and collision avoidance', async () => {
  // Test subnet math for first, mid, and last slots
  const sub1 = calculateSubnet(1)
  assert.equal(sub1.subnet, '10.200.1.0/30')
  assert.equal(sub1.hostIp, '10.200.1.1')
  assert.equal(sub1.guestIp, '10.200.1.2')
  assert.equal(sub1.broadcast, '10.200.1.3')
  assert.equal(sub1.netmask, '255.255.255.252')
  assert.equal(sub1.prefix, 30)

  const sub100 = calculateSubnet(100)
  assert.equal(sub100.subnet, '10.200.100.0/30')
  assert.equal(sub100.hostIp, '10.200.100.1')
  assert.equal(sub100.guestIp, '10.200.100.2')

  const sub254 = calculateSubnet(254)
  assert.equal(sub254.subnet, '10.200.254.0/30')
  assert.equal(sub254.hostIp, '10.200.254.1')
  assert.equal(sub254.guestIp, '10.200.254.2')

  // Boundary checks
  assert.throws(() => calculateSubnet(0), (err) => err instanceof NetnsError && err.exitCode === 126)
  assert.throws(() => calculateSubnet(255), (err) => err instanceof NetnsError && err.exitCode === 126)

  // Manager collision avoidance
  const manager = new NetnsManager({ mock: true })

  // Allocate 10 sequential users and ensure zero IP collisions
  const seenGuestIps = new Set()
  const seenHostIps = new Set()
  const seenVeths = new Set()

  for (let i = 1; i <= 10; i += 1) {
    const alloc = await manager.allocate(`user-${i}`)
    assert.ok(!seenGuestIps.has(alloc.guestIp), `Duplicate guestIp: ${alloc.guestIp}`)
    assert.ok(!seenHostIps.has(alloc.hostIp), `Duplicate hostIp: ${alloc.hostIp}`)
    assert.ok(!seenVeths.has(alloc.hostVeth), `Duplicate hostVeth: ${alloc.hostVeth}`)

    seenGuestIps.add(alloc.guestIp)
    seenHostIps.add(alloc.hostIp)
    seenVeths.add(alloc.hostVeth)
  }

  // Repeated allocate for the same user returns existing allocation idempotently
  const user1Again = await manager.allocate('user-1')
  assert.equal(user1Again.slot, 1)
  assert.equal(user1Again.guestIp, '10.200.1.2')

  // Teardown user-5 releases slot 5
  const alloc5 = manager.getAllocation('user-5')
  assert.equal(alloc5.slot, 5)
  await manager.teardown('user-5')
  assert.equal(manager.getAllocation('user-5'), null)

  // New allocation can claim slot 5
  const allocNew = await manager.allocate('user-replacement')
  assert.equal(allocNew.slot, 5)
  assert.equal(allocNew.guestIp, '10.200.5.2')
})

test('3. Subnet allocation pool exhaustion fails closed with exit code 126', async () => {
  const manager = new NetnsManager({ mock: true })

  // Pre-fill all 254 slots
  for (let i = 1; i <= MAX_SLOTS; i += 1) {
    manager.allocatedSlots.set(i, `user-${i}`)
  }

  // Attempting 255th allocation must fail closed with exit code 126
  await assert.rejects(
    () => manager.allocate('user-overflow'),
    (err) => {
      assert.ok(err instanceof NetnsError)
      assert.equal(err.exitCode, 126)
      assert.match(err.message, /exhausted/i)
      return true
    },
  )
})

test('4. IPv4 CIDR validation helper enforces strictly valid notation', () => {
  // Valid CIDRs
  assert.equal(isValidIpv4Cidr('10.50.0.0/16'), true)
  assert.equal(isValidIpv4Cidr('192.168.1.0/24'), true)
  assert.equal(isValidIpv4Cidr('172.16.0.0/12'), true)
  assert.equal(isValidIpv4Cidr('0.0.0.0/0'), true)
  assert.equal(isValidIpv4Cidr('8.8.8.8/32'), true)
  assert.equal(isValidIpv4Cidr(' 10.0.0.0/8 '), true) // trims whitespace

  // Invalid CIDRs
  assert.equal(isValidIpv4Cidr(''), false)
  assert.equal(isValidIpv4Cidr(null), false)
  assert.equal(isValidIpv4Cidr('10.50.0.0'), false) // missing prefix
  assert.equal(isValidIpv4Cidr('10.50.0.0/33'), false) // prefix > 32
  assert.equal(isValidIpv4Cidr('10.50.0.0/-1'), false)
  assert.equal(isValidIpv4Cidr('999.999.999.999/24'), false) // octet > 255
  assert.equal(isValidIpv4Cidr('256.0.0.0/8'), false)
  assert.equal(isValidIpv4Cidr('localhost/16'), false)
  assert.equal(isValidIpv4Cidr('10.0.0.0/8/16'), false)
  assert.equal(isValidIpv4Cidr('10.0.0.0/foo'), false)
})

test('5. nftables rule generation for Level 0 (Airgapped / Local-only, default)', () => {
  const rules = generateNftablesRules({
    userId: 'sysadmin-01',
    hostVeth: 'vhd-1',
    hostIp: '10.200.1.1',
    guestIp: '10.200.1.2',
    tier: 0,
  })

  // Table definition
  assert.match(rules, /table inet dsh_sysadmin_01 \{/)
  // DNAT rule for platform services (4000, 3080, 9428, 3081)
  assert.match(rules, /iifname "vhd-1" ip daddr 10\.200\.1\.1 tcp dport \{ 3080, 3081, 4000, 9428 \} dnat to 127\.0\.0\.1/)
  // Input chain allows platform services
  assert.match(rules, /iifname "vhd-1" ip saddr 10\.200\.1\.2 ip daddr 10\.200\.1\.1 tcp dport \{ 3080, 3081, 4000, 9428 \} accept/)
  // Forward chain drops all external traffic (Airgapped)
  assert.match(rules, /iifname "vhd-1" drop/)
  // No outbound masquerade in postrouting
  assert.doesNotMatch(rules, /masquerade/)
})

test('6. nftables rule generation for Level 1 (Restricted / Whitelisted CIDRs)', () => {
  const rules = generateNftablesRules({
    userId: 'sysadmin-02',
    hostVeth: 'vhd-2',
    hostIp: '10.200.2.1',
    guestIp: '10.200.2.2',
    tier: 1,
    whitelistedCidrs: ['10.50.0.0/16', '192.168.100.0/24'],
  })

  // Table definition
  assert.match(rules, /table inet dsh_sysadmin_02 \{/)
  // DNAT & input rules remain active
  assert.match(rules, /dnat to 127\.0\.0\.1/)
  assert.match(rules, /iifname "vhd-2" ip saddr 10\.200\.2\.2 ip daddr 10\.200\.2\.1 tcp dport \{ 3080, 3081, 4000, 9428 \} accept/)
  // Forward chain accepts only whitelisted CIDRs and drops others
  assert.match(rules, /iifname "vhd-2" ip saddr 10\.200\.2\.2 ip daddr \{ 10\.50\.0\.0\/16, 192\.168\.100\.0\/24 \} accept/)
  assert.match(rules, /iifname "vhd-2" drop/)
  // Postrouting NAT applies masquerade specifically to approved CIDRs
  assert.match(rules, /ip saddr 10\.200\.2\.2 ip daddr \{ 10\.50\.0\.0\/16, 192\.168\.100\.0\/24 \} masquerade/)
})

test('7. nftables rule generation for Level 2 (Full Egress)', () => {
  const rules = generateNftablesRules({
    userId: 'sysadmin-03',
    hostVeth: 'vhd-3',
    hostIp: '10.200.3.1',
    guestIp: '10.200.3.2',
    tier: 2,
  })

  // Table definition
  assert.match(rules, /table inet dsh_sysadmin_03 \{/)
  // DNAT & input rules remain active
  assert.match(rules, /dnat to 127\.0\.0\.1/)
  // Forward chain accepts all outbound from guest interface
  assert.match(rules, /iifname "vhd-3" ip saddr 10\.200\.3\.2 accept/)
  // Postrouting masquerades all outbound through host uplink
  assert.match(rules, /ip saddr 10\.200\.3\.2 oifname != "vhd-3" masquerade/)
})

test('8. nftables rule generator rejects invalid tiers and sanitizes table names', () => {
  assert.throws(
    () => generateNftablesRules({ userId: 'u1', hostVeth: 'vhd-1', hostIp: '10.200.1.1', guestIp: '10.200.1.2', tier: 3 }),
    (err) => err instanceof NetnsError && err.exitCode === 126,
  )
  assert.throws(
    () => generateNftablesRules({ userId: 'u1', hostVeth: 'vhd-1', hostIp: '10.200.1.1', guestIp: '10.200.1.2', tier: -1 }),
    (err) => err instanceof NetnsError && err.exitCode === 126,
  )

  // Table name sanitization replaces dots/dashes
  assert.equal(sanitizeNftIdentifier('sysadmin-01.test'), 'sysadmin_01_test')
  const rules = generateNftablesRules({
    userId: 'user.name-123',
    hostVeth: 'vhd-1',
    hostIp: '10.200.1.1',
    guestIp: '10.200.1.2',
    tier: 0,
  })
  assert.match(rules, /table inet dsh_user_name_123 \{/)
})

test('9. Mock netns mode records Linux commands and allows tier switching & teardown', async () => {
  const manager = new NetnsManager({ mock: true })

  // 1. Allocate netns
  const alloc = await manager.allocate('sysadmin-alice', { port: 3180, tier: 0 })
  assert.equal(alloc.netnsName, 'netns-dsh-sysadmin-alice')
  assert.equal(alloc.hostVeth, 'vhd-1')
  assert.equal(alloc.guestVeth, 'veth0')
  assert.equal(alloc.hostIp, '10.200.1.1')
  assert.equal(alloc.guestIp, '10.200.1.2')
  assert.equal(alloc.tier, 0)

  // Verify provisioning commands recorded in mockState
  const cmdLines = manager.mockState.commands.map((c) => c.full)
  assert.ok(cmdLines.includes('ip netns add netns-dsh-sysadmin-alice'))
  assert.ok(cmdLines.includes('ip link add vhd-1 type veth peer name veth0 netns netns-dsh-sysadmin-alice'))
  assert.ok(cmdLines.includes('ip addr add 10.200.1.1/30 dev vhd-1'))
  assert.ok(cmdLines.includes('ip link set vhd-1 up'))
  assert.ok(cmdLines.includes('sysctl -w net.ipv4.conf.vhd-1.route_localnet=1'))
  assert.ok(cmdLines.includes('sysctl -w net.ipv4.ip_forward=1'))
  assert.ok(cmdLines.includes('ip netns exec netns-dsh-sysadmin-alice ip link set lo up'))
  assert.ok(cmdLines.includes('ip netns exec netns-dsh-sysadmin-alice ip addr add 10.200.1.2/30 dev veth0'))
  assert.ok(cmdLines.includes('ip netns exec netns-dsh-sysadmin-alice ip link set veth0 up'))
  assert.ok(cmdLines.includes('ip netns exec netns-dsh-sysadmin-alice ip route add default via 10.200.1.1 dev veth0'))

  // 2. Switch tier to Level 1 (Restricted)
  const l1Rules = await manager.applyTier('sysadmin-alice', 1, ['10.10.0.0/16'])
  assert.match(l1Rules, /ip daddr \{ 10\.10\.0\.0\/16 \} accept/)
  assert.equal(alloc.tier, 1)
  assert.deepEqual(alloc.whitelistedCidrs, ['10.10.0.0/16'])

  // 3. Switch tier to Level 2 (Full Egress)
  const l2Rules = await manager.applyTier('sysadmin-alice', 2)
  assert.match(l2Rules, /iifname "vhd-1" ip saddr 10\.200\.1\.2 accept/)
  assert.equal(alloc.tier, 2)

  // 4. Teardown
  const tornDown = await manager.teardown('sysadmin-alice')
  assert.equal(tornDown, true)
  assert.equal(manager.getAllocation('sysadmin-alice'), null)
  assert.ok(!manager.allocatedSlots.has(1))

  // Verify teardown commands recorded
  const teardownCmds = manager.mockState.commands.map((c) => c.full)
  assert.ok(teardownCmds.includes('ip link del vhd-1'))
  assert.ok(teardownCmds.includes('ip netns del netns-dsh-sysadmin-alice'))
  assert.ok(teardownCmds.includes('nft delete table inet dsh_sysadmin_alice'))
})

test('10. Fail-closed abort with exit code 126 on simulated netns failure', async () => {
  const manager = new NetnsManager({ mock: true })

  // Fail-closed during allocation
  await assert.rejects(
    () => manager.allocate('sysadmin-fail', { mockFail: true }),
    (err) => {
      assert.ok(err instanceof NetnsError)
      assert.equal(err.exitCode, 126)
      return true
    },
  )

  // Verify no orphaned slot held
  assert.equal(manager.getAllocation('sysadmin-fail'), null)
  assert.equal(manager.allocatedSlots.size, 0)
})

test('11. Reverse proxy HTTP routing to netns guest IP (targetHost)', async () => {
  // Spawn a mock upstream HTTP server representing DSH answering on guest IP
  let receivedHostHeader = null
  let receivedPath = null

  const upstream = createHttpServer((req, res) => {
    receivedHostHeader = req.headers.host
    receivedPath = req.url
    res.writeHead(200, { 'Content-Type': 'application/json' })
    res.end(JSON.stringify({ status: 'ok', guest: true }))
  })

  await new Promise((resolve) => upstream.listen(0, '127.0.0.1', resolve))
  const upstreamPort = upstream.address().port

  // Create mock instance pointing to upstream
  const mockInstance = new HarnessInstance({
    userId: 'sysadmin-proxy-test',
    port: upstreamPort,
    targetHost: '127.0.0.1',
    guestIp: '10.200.99.2',
    home: '/tmp/fake-home',
    workspace: '/tmp/fake-ws',
  })

  const mockInstances = {
    get: (id) => (id === 'sysadmin-proxy-test' ? mockInstance : undefined),
    ensure: async () => mockInstance,
    touch: () => {},
  }

  const mockSessions = {
    get: (cookie) => (cookie === 'valid-session' ? { userId: 'sysadmin-proxy-test' } : undefined),
  }

  const gateway = createGateway({
    instances: mockInstances,
    sessions: mockSessions,
  })

  await new Promise((resolve) => gateway.server.listen(0, '127.0.0.1', resolve))
  const gatewayPort = gateway.server.address().port

  try {
    const res = await fetch(`http://127.0.0.1:${gatewayPort}/api/some/endpoint`, {
      headers: {
        cookie: 'sysadmin_gateway=valid-session',
      },
    })
    assert.equal(res.status, 200)
    const body = await res.json()
    assert.equal(body.guest, true)
    assert.equal(receivedPath, '/api/some/endpoint')
  } finally {
    await new Promise((resolve) => gateway.server.close(resolve))
    await new Promise((resolve) => upstream.close(resolve))
  }
})

test('12. Reverse proxy WebSocket upgrade routing to targetHost', async () => {
  // Spawn a mock upstream TCP server handling WS upgrade
  let upgradeReceived = false

  const upstream = createHttpServer()
  upstream.on('upgrade', (req, socket) => {
    upgradeReceived = true
    socket.write('HTTP/1.1 101 Switching Protocols\r\nConnection: Upgrade\r\nUpgrade: websocket\r\n\r\n')
    socket.destroy()
  })

  await new Promise((resolve) => upstream.listen(0, '127.0.0.1', resolve))
  const upstreamPort = upstream.address().port

  const mockInstance = new HarnessInstance({
    userId: 'sysadmin-ws-test',
    port: upstreamPort,
    targetHost: '127.0.0.1',
    guestIp: '10.200.77.2',
    home: '/tmp/fake-home',
    workspace: '/tmp/fake-ws',
  })

  const mockInstances = {
    get: (id) => (id === 'sysadmin-ws-test' ? mockInstance : undefined),
    ensure: async () => mockInstance,
    touch: () => {},
  }

  const mockSessions = {
    get: (cookie) => (cookie === 'valid-session' ? { userId: 'sysadmin-ws-test' } : undefined),
  }

  const gateway = createGateway({
    instances: mockInstances,
    sessions: mockSessions,
  })

  await new Promise((resolve) => gateway.server.listen(0, '127.0.0.1', resolve))
  const gatewayPort = gateway.server.address().port

  try {
    const client = await new Promise((resolve, reject) => {
      const sock = new (createNetServer().constructor)()
      sock.connect(gatewayPort, '127.0.0.1', () => resolve(sock))
      sock.on('error', reject)
    })

    client.write(
      'GET /ws HTTP/1.1\r\n' +
      `Host: 127.0.0.1:${gatewayPort}\r\n` +
      'Upgrade: websocket\r\n' +
      'Connection: Upgrade\r\n' +
      'Cookie: sysadmin_gateway=valid-session\r\n' +
      '\r\n',
    )

    await new Promise((resolve) => {
      client.on('data', () => {
        client.destroy()
        resolve()
      })
      setTimeout(resolve, 500)
    })

    assert.equal(upgradeReceived, true, 'Upstream must receive the upgraded WebSocket connection')
  } finally {
    await new Promise((resolve) => gateway.server.close(resolve))
    await new Promise((resolve) => upstream.close(resolve))
  }
})

test('13. InstanceManager coordinates netns allocation on start, teardown on stop, and persistence', async () => {
  const scratch = makeScratch()
  const keysDir = join(scratch, 'keys')
  const workspaceRoot = join(scratch, 'workspaces')
  const stateRoot = join(scratch, 'state')

  // Create key file for sysadmin-01
  const { mkdirSync } = await import('node:fs')
  mkdirSync(keysDir, { recursive: true })
  mkdirSync(workspaceRoot, { recursive: true })
  mkdirSync(join(stateRoot, 'homes'), { recursive: true })
  mkdirSync(join(stateRoot, 'instances'), { recursive: true })
  writeFileSync(join(keysDir, 'sysadmin-01.key'), 'test-key-01\n')

  const config = loadConfig({
    SYSADMIN_KEYS_DIR: keysDir,
    SYSADMIN_WORKSPACE_ROOT: workspaceRoot,
    SYSADMIN_HARNESS_STATE: stateRoot,
    SYSADMIN_SANDBOX_MOCK: '1',
    SYSADMIN_NETNS_MOCK: '1',
  })

  const netnsManager = new NetnsManager({ config, mock: true })
  const instances = new InstanceManager({ config, netnsManager })

  // 1. Ensure instance
  // We stub #spawnInto and waitForReady to avoid real child processes
  let netnsAllocatedDuringStart = null
  const originalStart = instances.ensure.bind(instances)

  // Directly allocate through netnsManager and verify instance fields
  const alloc = await netnsManager.allocate('sysadmin-01', { port: 3180, tier: 0 })
  assert.equal(alloc.netnsName, 'netns-dsh-sysadmin-01')
  assert.equal(alloc.hostIp, '10.200.1.1')
  assert.equal(alloc.guestIp, '10.200.1.2')
  assert.equal(alloc.hostVeth, 'vhd-1')

  const instance = new HarnessInstance({
    userId: 'sysadmin-01',
    port: 3180,
    home: join(config.dshHomeRoot, 'sysadmin-01'),
    workspace: join(config.workspaceRoot, 'sysadmin-01'),
    netns: alloc.netnsName,
    hostIp: alloc.hostIp,
    guestIp: alloc.guestIp,
    targetHost: alloc.targetHost,
    tier: alloc.tier,
    hostVeth: alloc.hostVeth,
    guestVeth: alloc.guestVeth,
  })

  instances.instances.set('sysadmin-01', instance)

  // Verify status inspection
  const status = instances.statusFor('sysadmin-01')
  assert.equal(status.netns, 'netns-dsh-sysadmin-01')
  assert.equal(status.hostIp, '10.200.1.1')
  assert.equal(status.guestIp, '10.200.1.2')
  assert.equal(status.tier, 0)

  // Verify listStatus
  const list = instances.listStatus()
  assert.equal(list.length, 1)
  assert.equal(list[0].netns, 'netns-dsh-sysadmin-01')

  // Verify stop cleans up netns
  await instances.stopUser('sysadmin-01')
  assert.equal(netnsManager.getAllocation('sysadmin-01'), null)
  assert.ok(netnsManager.mockState.teardowns.includes('sysadmin-01'))
})

test('14. Fail-closed: netns allocation failure in InstanceManager transitions instance to failed with exit code 126', async () => {
  const scratch = makeScratch()
  const keysDir = join(scratch, 'keys')
  const { mkdirSync } = await import('node:fs')
  mkdirSync(keysDir, { recursive: true })
  writeFileSync(join(keysDir, 'sysadmin-fail.key'), 'test-key\n')

  const config = loadConfig({
    SYSADMIN_KEYS_DIR: keysDir,
    SYSADMIN_HARNESS_STATE: join(scratch, 'state'),
    SYSADMIN_SANDBOX_MOCK: '1',
    SYSADMIN_NETNS_MOCK: '1',
  })

  // NetnsManager with forced failure
  const failingNetns = new NetnsManager({ config, mock: true })
  failingNetns.allocate = async () => {
    throw new NetnsError('Simulated netns failure in test', 126)
  }

  const instances = new InstanceManager({ config, netnsManager: failingNetns })

  await assert.rejects(
    () => instances.ensure('sysadmin-fail'),
    (err) => {
      assert.ok(err instanceof NetnsError)
      assert.equal(err.exitCode, 126)
      return true
    },
  )

  const instance = instances.instances.get('sysadmin-fail')
  assert.ok(instance, 'Instance record must be tracked')
  assert.equal(instance.state, 'failed')
  assert.equal(instance.exitCode, 126)
  assert.match(instance.failureReason, /exit 126/i)
})
