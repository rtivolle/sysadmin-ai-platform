# SeaweedFS Distributed Storage Maintenance Runbook

## 1. Cluster Status
Check master state at `http://127.0.0.1:9333/cluster/status`.

## 2. Volume Vacuum and Compaction
When disk usage remains high after deleting objects:
1. Connect to master shell:
   `weed shell -master=127.0.0.1:9333`
2. Execute volume vacuuming with garbage threshold:
   `volume.vacuum -garbageThreshold=0.3`
3. Explanation: This command compacts volume files and physically releases freed disk blocks.

## 3. Filer Replication
Inspect replication lag in filer metadata.
