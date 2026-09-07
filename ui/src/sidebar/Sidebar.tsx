import { RosterSection } from './RosterSection'
import { PlanSection } from './PlanSection'
import { RoutingSection } from './RoutingSection'
import { CostSection } from './CostSection'

/** The four `aside` sections from mockups-next/derate.html: nodes, the
 *  scoped plan, that deployment's routing, and its cost per Mtok. Each
 *  section owns its own data -- there is nothing to pass down here. */
export function Sidebar() {
  return (
    <>
      <RosterSection />
      <PlanSection />
      <RoutingSection />
      <CostSection />
    </>
  )
}
