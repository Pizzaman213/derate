import { useCluster, useReplication, useStorage, useTransfers } from '../../state/resources'
import { FilesystemsCard } from './FilesystemsCard'
import { ReplicationCard } from './ReplicationCard'
import { ModelPresenceCard } from './ModelPresenceCard'
import { ModelCacheCard } from './ModelCacheCard'
import { EstateCard } from './EstateCard'
import { CollectionCard } from './CollectionCard'

/** Settings → Storage: the six cards, and the ONE subscription to each feed
 *  they share.
 *
 *  `useResource` does not deduplicate, so a card that mounts its own hook is
 *  its own poll: `/api/storage` was read by four cards (each a fan-out to
 *  every node agent and a directory walk on each) and `/api/replication` by
 *  two. Every hook is called here once and the result is passed down, so a
 *  tick is one request per endpoint, and every card on the screen draws the
 *  same answer. */
export function StorageSection() {
  const storage = useStorage()
  const cluster = useCluster()
  const replication = useReplication()
  const transfers = useTransfers()
  const coordinatorId =
    cluster.data?.nodes.find((n) => n.role === 'coordinator')?.profile.node_id ?? null

  return (
    <>
      <FilesystemsCard storage={storage} />
      <ReplicationCard replication={replication} />
      <ModelPresenceCard
        storage={storage}
        replication={replication}
        transfers={transfers}
        coordinatorId={coordinatorId}
      />
      <ModelCacheCard storage={storage} cluster={cluster} replication={replication} />
      <EstateCard storage={storage} />
      <CollectionCard storage={storage} />
    </>
  )
}
