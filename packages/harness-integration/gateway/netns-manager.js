/**
 * Per-user Linux Network Namespace and Egress Access Tier Manager (ADR-0005, R2).
 *
 * Provides dedicated network namespace isolation (netns-dsh-<userId>), veth pair
 * interconnect (respecting Linux 15-char IFNAMSIZ), deterministic /30 subnet allocation
 * under 10.200.0.0/16, platform services loopback bridging (ports 4000, 3080, 9428, 3081),
 * and three admin-controlled nftables egress access tiers:
 *
 *   - Level 0 (Airgapped / Local-only, default): traffic permitted only to host platform
 *     services; all external routing and public IPs dropped.
 *   - Level 1 (Restricted / Whitelisted Egress): platform services plus admin-specified CIDR blocks.
 *   - Level 2 (Full Egress): outbound internet access via host uplink SNAT/masquerade.
 *
 * In unprivileged environments (macOS / Darwin, SYSADMIN_NETNS_MOCK=1, or non-root test
 * runners), operates in mock mode with full command and state recording and fail-closed
 * exit 126 semantics.
 */
import { execFileSync } from 'node:child_process'
import { assertSafeUserId } from './instance-manager.js'

export const PLATFORM_SERVICE_PORTS = [3080, 3081, 4000, 9428]
export const SUBNET_BASE_PREFIX = '10.200'
export const MAX_SLOTS = 254

export class NetnsError extends Error {
  /**
   * @param {string} message
   * @param {number} [exitCode]
   */
  constructor(message, exitCode = 126) {
    super(message)
    this.name = 'NetnsError'
    this.exitCode = exitCode
  }
}

/**
 * Validate an IPv4 CIDR notation string (e.g. "10.50.0.0/16").
 *
 * @param {string} cidr
 * @returns {boolean}
 */
export function isValidIpv4Cidr(cidr) {
  if (typeof cidr !== 'string') return false
  const match = cidr.trim().match(/^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})\/(\d{1,2})$/)
  if (!match) return false
  const [, a, b, c, d, prefix] = match
  const octets = [Number(a), Number(b), Number(c), Number(d)]
  if (octets.some((o) => o < 0 || o > 255)) return false
  const p = Number(prefix)
  if (p < 0 || p > 32) return false
  return true
}

/**
 * Format Linux network interface names respecting the 15-char IFNAMSIZ limit.
 *
 * @param {number} slotIdx
 * @returns {{ hostVeth: string, guestVeth: string }}
 */
export function formatInterfaceNames(slotIdx) {
  if (!Number.isInteger(slotIdx) || slotIdx < 1 || slotIdx > MAX_SLOTS) {
    throw new NetnsError(`Invalid slot index ${slotIdx}: must be between 1 and ${MAX_SLOTS}`, 126)
  }
  const hostVeth = `vhd-${slotIdx}`
  const guestVeth = 'veth0'
  if (hostVeth.length > 15) {
    throw new NetnsError(`Host interface name "${hostVeth}" exceeds Linux 15-char IFNAMSIZ limit`, 126)
  }
  return { hostVeth, guestVeth }
}

/**
 * Compute deterministic /30 point-to-point subnet addressing under 10.200.0.0/16.
 *
 * @param {number} slotIdx
 * @returns {{ subnet: string, hostIp: string, guestIp: string, broadcast: string, netmask: string, prefix: number }}
 */
export function calculateSubnet(slotIdx) {
  if (!Number.isInteger(slotIdx) || slotIdx < 1 || slotIdx > MAX_SLOTS) {
    throw new NetnsError(`Invalid slot index ${slotIdx}: must be between 1 and ${MAX_SLOTS}`, 126)
  }
  return {
    subnet: `${SUBNET_BASE_PREFIX}.${slotIdx}.0/30`,
    hostIp: `${SUBNET_BASE_PREFIX}.${slotIdx}.1`,
    guestIp: `${SUBNET_BASE_PREFIX}.${slotIdx}.2`,
    broadcast: `${SUBNET_BASE_PREFIX}.${slotIdx}.3`,
    netmask: '255.255.255.252',
    prefix: 30,
  }
}

/**
 * Format safe nftables identifier from userId.
 *
 * @param {string} userId
 * @returns {string}
 */
export function sanitizeNftIdentifier(userId) {
  return userId.replace(/[^A-Za-z0-9_]/g, '_')
}

/**
 * Generate nftables rule definition for the specified tier.
 *
 * @param {object} params
 * @param {string} params.userId
 * @param {string} params.hostVeth
 * @param {string} params.hostIp
 * @param {string} params.guestIp
 * @param {number} params.tier 0, 1, or 2
 * @param {string[]} [params.whitelistedCidrs]
 * @returns {string}
 */
export function generateNftablesRules({
  userId,
  hostVeth,
  hostIp,
  guestIp,
  tier,
  whitelistedCidrs = [],
}) {
  const safeId = sanitizeNftIdentifier(userId)
  const portsStr = PLATFORM_SERVICE_PORTS.join(', ')

  if (tier !== 0 && tier !== 1 && tier !== 2) {
    throw new NetnsError(`Invalid network access tier: ${tier} (must be 0, 1, or 2)`, 126)
  }

  const validCidrs = (whitelistedCidrs ?? []).filter(isValidIpv4Cidr)
  if (tier === 1 && validCidrs.length === 0) {
    // If Level 1 requested with no valid CIDRs, log or treat as airgapped
  }

  let forwardRule = ''
  let postroutingRule = ''

  if (tier === 0) {
    // Level 0: Airgapped / Local-only. Drop all external forwarding.
    forwardRule = `    iifname "${hostVeth}" drop`
    postroutingRule = `    # Level 0: No outbound masquerade`
  } else if (tier === 1) {
    // Level 1: Restricted / Whitelisted CIDRs.
    if (validCidrs.length > 0) {
      const cidrSet = validCidrs.join(', ')
      forwardRule = `    iifname "${hostVeth}" ip saddr ${guestIp} ip daddr { ${cidrSet} } accept\n    iifname "${hostVeth}" drop`
      postroutingRule = `    ip saddr ${guestIp} ip daddr { ${cidrSet} } masquerade`
    } else {
      forwardRule = `    iifname "${hostVeth}" drop`
      postroutingRule = `    # Level 1: No CIDRs defined, dropping forwarding`
    }
  } else if (tier === 2) {
    // Level 2: Full Egress via host uplink.
    forwardRule = `    iifname "${hostVeth}" ip saddr ${guestIp} accept`
    postroutingRule = `    ip saddr ${guestIp} oifname != "${hostVeth}" masquerade`
  }

  return `table inet dsh_${safeId} {
  chain prerouting {
    type nat hook prerouting priority dstnat; policy accept;
    iifname "${hostVeth}" ip daddr ${hostIp} tcp dport { ${portsStr} } dnat to 127.0.0.1
  }

  chain input {
    type filter hook input priority filter; policy drop;
    iifname "${hostVeth}" ip saddr ${guestIp} ip daddr ${hostIp} tcp dport { ${portsStr} } accept
    iifname "${hostVeth}" ct state established,related accept
    iifname "${hostVeth}" drop
  }

  chain forward {
    type filter hook forward priority filter; policy drop;
    ct state established,related accept
${forwardRule}
  }

  chain postrouting {
    type nat hook postrouting priority srcnat; policy accept;
${postroutingRule}
  }
}
`
}

export class NetnsManager {
  /**
   * @param {object} [options]
   * @param {ReturnType<import('./config.js').loadConfig>} [options.config]
   * @param {(message: string) => void} [options.logger]
   * @param {boolean} [options.mock]
   * @param {(cmd: string, args: string[], opts?: object) => string|Buffer} [options.executor]
   */
  constructor({ config, logger = () => {}, mock = null, executor = null } = {}) {
    this.config = config
    this.log = logger
    this.isMock = mock ?? (
      config?.netnsMock
      ?? (process.env.SYSADMIN_NETNS_MOCK === '1' || process.platform === 'darwin')
    )
    this.executor = executor ?? this.#defaultExecutor.bind(this)

    /** @type {Map<number, string>} slotIdx -> userId */
    this.allocatedSlots = new Map()

    /** @type {Map<string, object>} userId -> allocation */
    this.userAllocations = new Map()

    /** Mock state for testing & inspection */
    this.mockState = {
      commands: [],
      tables: new Map(),
      namespaces: new Map(),
      teardowns: [],
    }
  }

  #defaultExecutor(cmd, args, opts = {}) {
    return execFileSync(cmd, args, { stdio: 'pipe', encoding: 'utf8', ...opts })
  }

  /**
   * Allocate resources for a user's network namespace.
   *
   * @param {string} userId
   * @param {object} [options]
   * @param {number|null} [options.port]
   * @param {number|null} [options.preferredSlot]
   * @param {number} [options.tier] 0, 1, 2
   * @param {string[]} [options.whitelistedCidrs]
   * @param {boolean} [options.mockFail]
   * @returns {Promise<object>}
   */
  async allocate(userId, options = {}) {
    assertSafeUserId(userId)

    if (options.mockFail || process.env.MOCK_NETNS_FAIL === '1') {
      throw new NetnsError('Simulated netns allocation failure (fail-closed exit 126)', 126)
    }

    // Reuse existing allocation if present and still valid
    const existing = this.userAllocations.get(userId)
    if (existing) {
      return existing
    }

    // Choose slot: prefer preferredSlot if free, else port-based or lowest free slot
    let slot = null
    if (
      options.preferredSlot
      && options.preferredSlot >= 1
      && options.preferredSlot <= MAX_SLOTS
      && !this.allocatedSlots.has(options.preferredSlot)
    ) {
      slot = options.preferredSlot
    } else if (
      options.port
      && this.config?.instancePortStart
      && (options.port - this.config.instancePortStart + 1) >= 1
      && (options.port - this.config.instancePortStart + 1) <= MAX_SLOTS
      && !this.allocatedSlots.has(options.port - this.config.instancePortStart + 1)
    ) {
      slot = options.port - this.config.instancePortStart + 1
    } else {
      for (let s = 1; s <= MAX_SLOTS; s += 1) {
        if (!this.allocatedSlots.has(s)) {
          slot = s
          break
        }
      }
    }

    if (!slot) {
      throw new NetnsError('Subnet allocation pool exhausted under 10.200.0.0/16', 126)
    }

    const { hostVeth, guestVeth } = formatInterfaceNames(slot)
    const subnetInfo = calculateSubnet(slot)
    const netnsName = `netns-dsh-${userId}`
    const tier = options.tier ?? this.config?.defaultNetworkTier ?? 0
    const whitelistedCidrs = (options.whitelistedCidrs ?? []).filter(isValidIpv4Cidr)

    const allocation = {
      userId,
      slot,
      netnsName,
      hostVeth,
      guestVeth,
      subnet: subnetInfo.subnet,
      hostIp: subnetInfo.hostIp,
      guestIp: subnetInfo.guestIp,
      broadcast: subnetInfo.broadcast,
      netmask: subnetInfo.netmask,
      tier,
      whitelistedCidrs,
      targetHost: this.isMock ? '127.0.0.1' : subnetInfo.guestIp,
      isMock: this.isMock,
      createdAt: Date.now(),
    }

    try {
      if (this.isMock) {
        this.#recordMockProvisioning(allocation)
      } else {
        this.#executeLinuxProvisioning(allocation)
      }
    } catch (err) {
      this.teardown(userId).catch(() => {})
      const msg = err instanceof Error ? err.message : String(err)
      throw new NetnsError(`Failed to provision network namespace for ${userId}: ${msg}`, 126)
    }

    this.allocatedSlots.set(slot, userId)
    this.userAllocations.set(userId, allocation)
    this.log(`allocated netns ${netnsName} (${subnetInfo.guestIp}) on ${hostVeth} for ${userId} (tier ${tier})`)
    return allocation
  }

  /**
   * Apply or update the egress network tier for a user.
   *
   * @param {string} userId
   * @param {number} tier
   * @param {string[]} [whitelistedCidrs]
   * @returns {Promise<string>} Generated nftables ruleset
   */
  async applyTier(userId, tier, whitelistedCidrs = []) {
    assertSafeUserId(userId)
    const alloc = this.userAllocations.get(userId)
    if (!alloc) {
      throw new NetnsError(`Cannot apply network tier: no active allocation for ${userId}`, 126)
    }

    if (tier !== 0 && tier !== 1 && tier !== 2) {
      throw new NetnsError(`Invalid network tier ${tier}: must be 0, 1, or 2`, 126)
    }

    const validCidrs = (whitelistedCidrs ?? []).filter(isValidIpv4Cidr)
    const rules = generateNftablesRules({
      userId,
      hostVeth: alloc.hostVeth,
      hostIp: alloc.hostIp,
      guestIp: alloc.guestIp,
      tier,
      whitelistedCidrs: validCidrs,
    })

    try {
      if (this.isMock) {
        this.mockState.commands.push({
          cmd: 'nft',
          args: ['-f', '-'],
          input: rules,
          full: `nft -f - <<EOF\n${rules}EOF`,
        })
        this.mockState.tables.set(userId, rules)
      } else {
        this.executor('nft', ['-f', '-'], { input: rules })
      }
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err)
      throw new NetnsError(`Failed to apply nftables tier ${tier} for ${userId}: ${msg}`, 126)
    }

    alloc.tier = tier
    alloc.whitelistedCidrs = validCidrs
    this.log(`applied network tier ${tier} for ${userId} (${validCidrs.length} CIDRs)`)
    return rules
  }

  /**
   * Teardown network namespace, interfaces, and packet filtering rules for a user.
   *
   * @param {string} userId
   * @returns {Promise<boolean>}
   */
  async teardown(userId) {
    assertSafeUserId(userId)
    const alloc = this.userAllocations.get(userId)
    if (!alloc) return false

    const safeId = sanitizeNftIdentifier(userId)

    if (this.isMock) {
      this.mockState.teardowns.push(userId)
      this.mockState.commands.push({ cmd: 'ip', args: ['link', 'del', alloc.hostVeth], full: `ip link del ${alloc.hostVeth}` })
      this.mockState.commands.push({ cmd: 'ip', args: ['netns', 'del', alloc.netnsName], full: `ip netns del ${alloc.netnsName}` })
      this.mockState.commands.push({ cmd: 'nft', args: ['delete', 'table', 'inet', `dsh_${safeId}`], full: `nft delete table inet dsh_${safeId}` })
      this.mockState.namespaces.delete(userId)
      this.mockState.tables.delete(userId)
    } else {
      // Best effort cleanup in reverse order
      try {
        this.executor('nft', ['delete', 'table', 'inet', `dsh_${safeId}`])
      } catch {
        /* best effort */
      }
      try {
        this.executor('ip', ['link', 'del', alloc.hostVeth])
      } catch {
        /* best effort */
      }
      try {
        this.executor('ip', ['netns', 'del', alloc.netnsName])
      } catch {
        /* best effort */
      }
    }

    this.allocatedSlots.delete(alloc.slot)
    this.userAllocations.delete(userId)
    this.log(`torn down netns ${alloc.netnsName} and host interface ${alloc.hostVeth} for ${userId}`)
    return true
  }

  /**
   * Returns target host and port for HTTP / WebSocket reverse proxying.
   *
   * @param {string} userId
   * @param {number} instancePort
   * @returns {{ host: string, port: number }}
   */
  getInstanceTarget(userId, instancePort) {
    const alloc = this.userAllocations.get(userId)
    if (!alloc) {
      return { host: this.config?.instanceHost ?? '127.0.0.1', port: instancePort }
    }
    return {
      host: alloc.targetHost || alloc.guestIp || '127.0.0.1',
      port: instancePort,
    }
  }

  /**
   * Query current allocation information.
   *
   * @param {string} userId
   * @returns {object|null}
   */
  getAllocation(userId) {
    return this.userAllocations.get(userId) ?? null
  }

  #recordMockProvisioning(alloc) {
    const cmds = [
      { cmd: 'ip', args: ['netns', 'add', alloc.netnsName] },
      { cmd: 'ip', args: ['link', 'add', alloc.hostVeth, 'type', 'veth', 'peer', 'name', alloc.guestVeth, 'netns', alloc.netnsName] },
      { cmd: 'ip', args: ['addr', 'add', `${alloc.hostIp}/30`, 'dev', alloc.hostVeth] },
      { cmd: 'ip', args: ['link', 'set', alloc.hostVeth, 'up'] },
      { cmd: 'sysctl', args: ['-w', `net.ipv4.conf.${alloc.hostVeth}.route_localnet=1`] },
      { cmd: 'sysctl', args: ['-w', 'net.ipv4.ip_forward=1'] },
      { cmd: 'ip', args: ['netns', 'exec', alloc.netnsName, 'ip', 'link', 'set', 'lo', 'up'] },
      { cmd: 'ip', args: ['netns', 'exec', alloc.netnsName, 'ip', 'addr', 'add', `${alloc.guestIp}/30`, 'dev', alloc.guestVeth] },
      { cmd: 'ip', args: ['netns', 'exec', alloc.netnsName, 'ip', 'link', 'set', alloc.guestVeth, 'up'] },
      { cmd: 'ip', args: ['netns', 'exec', alloc.netnsName, 'ip', 'route', 'add', 'default', 'via', alloc.hostIp, 'dev', alloc.guestVeth] },
    ]
    for (const c of cmds) {
      this.mockState.commands.push({ ...c, full: [c.cmd, ...c.args].join(' ') })
    }
    this.mockState.namespaces.set(alloc.userId, alloc)
  }

  #executeLinuxProvisioning(alloc) {
    this.executor('ip', ['netns', 'add', alloc.netnsName])
    this.executor('ip', ['link', 'add', alloc.hostVeth, 'type', 'veth', 'peer', 'name', alloc.guestVeth, 'netns', alloc.netnsName])
    this.executor('ip', ['addr', 'add', `${alloc.hostIp}/30`, 'dev', alloc.hostVeth])
    this.executor('ip', ['link', 'set', alloc.hostVeth, 'up'])
    this.executor('sysctl', ['-w', `net.ipv4.conf.${alloc.hostVeth}.route_localnet=1`])
    this.executor('sysctl', ['-w', 'net.ipv4.ip_forward=1'])
    this.executor('ip', ['netns', 'exec', alloc.netnsName, 'ip', 'link', 'set', 'lo', 'up'])
    this.executor('ip', ['netns', 'exec', alloc.netnsName, 'ip', 'addr', 'add', `${alloc.guestIp}/30`, 'dev', alloc.guestVeth])
    this.executor('ip', ['netns', 'exec', alloc.netnsName, 'ip', 'link', 'set', alloc.guestVeth, 'up'])
    this.executor('ip', ['netns', 'exec', alloc.netnsName, 'ip', 'route', 'add', 'default', 'via', alloc.hostIp, 'dev', alloc.guestVeth])
  }
}
