import { useResource } from './backend'

// Poll intervals. Structure changes slowly; discovery is the one thing a person
// is actively waiting on, so it is checked most often. Live numbers do not come
// from here at all — they come from the stream.

export const useCluster = () => useResource((b) => b.cluster(), 5000)
export const useTopology = () => useResource((b) => b.topology(), 5000)
export const useCandidates = () => useResource((b) => b.candidates(), 3000)
export const useRouting = () => useResource((b) => b.routing(), 5000)
export const useProviders = () => useResource((b) => b.providers(), 15000)
