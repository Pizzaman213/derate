import { RosterSection } from './RosterSection'
import { ActivitySection } from './ActivitySection'
import { PlanSection } from './PlanSection'
import { RoutingSection } from './RoutingSection'
import { CostSection } from './CostSection'

/** The `aside` sections. Four are ported from mockups-next/derate.html: nodes,
 *  the scoped plan, that deployment's routing, and its cost per Mtok. Each
 *  section owns its own data -- there is nothing to pass down here.
 *
 *  `Activity` is the fifth and is not from the mockup. It sits directly under
 *  the nodes and above the three that describe a settled cluster, because it
 *  answers the question those three cannot: the mockup had no state for "a
 *  model is on its way", so all three said "Nothing is being served." during a
 *  download. It renders nothing at all when nothing is arriving. */
export function Sidebar() {
  return (
    <>
      <RosterSection />
      <ActivitySection />
      <PlanSection />
      <RoutingSection />
      <CostSection />
    </>
  )
}
