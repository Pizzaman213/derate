import { FilesystemsCard } from './storage/FilesystemsCard'
import { ModelCacheCard } from './storage/ModelCacheCard'
import { EstateCard } from './storage/EstateCard'
import { CollectionCard } from './storage/CollectionCard'

/** The Storage destination: capacity, what this product is spending it on, and
 *  whether the durable record is actually durable.
 *
 *  Not in mockups-next -- there is no mockup ancestor for this one. Added
 *  2026-09-07 and recorded in 00-architecture.md's storage appendix; the
 *  composition follows SettingsTab, which is the house pattern for a
 *  management surface: a flat list of self-fetching cards.
 *
 *  Every number here is read on demand. Disk is deliberately not sampled
 *  anywhere in the product, so there is no history of it and nothing on this
 *  screen is a trace -- see registry/storage.py for why. */
export function StorageTab() {
  return (
    <div>
      <FilesystemsCard />
      <ModelCacheCard />
      <EstateCard />
      <CollectionCard />
    </div>
  )
}
