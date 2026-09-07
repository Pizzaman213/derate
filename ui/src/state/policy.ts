import type { RoutingPolicy } from '../api/types'

/** `weight` is only semantically a configured traffic SHARE under these two
 *  policies. Drawing a filled proportional bar under, say, least_outstanding
 *  would claim a configured split the router does not use for selection.
 *
 *  Shared by the routing sidebar and the cluster graph's per-machine share
 *  bar so the two can never disagree about when a weight means a share. */
export const PROPORTIONAL = new Set<RoutingPolicy>(['weighted_capacity', 'round_robin'])
