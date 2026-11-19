import type { ChatRole, ChatTurnMeta, SpeechResult, ThinkingChoice } from '../../api/types'
import type { ReasoningSetup } from './reasoning'

export interface Turn {
  key: number
  role: ChatRole
  content: string
  /** The model's reasoning, kept apart from `content` so it is never sent back
   *  as history. `''` on a user turn and before any arrives. */
  reasoning: string
  /** What the thinking control said when this turn was sent. `null` on user
   *  turns and audio turns, which have no such control. */
  requested: ThinkingChoice | null
  /** How the model it was sent to was set up to reason, at send time -- the
   *  verdict under the answer is about that deployment, not whichever model
   *  is picked when it is read. `null` on user and audio turns. */
  setup: ReasoningSetup | null
  /** Set when the turn finished normally, or was stopped. */
  meta: ChatTurnMeta | null
  /** The gateway's own sentence when it refused. Rendered verbatim. */
  error: string | null
  /** Known from the response headers, so an errored turn still names the row
   *  that recorded it. */
  requestId: string | null
  /** The answer, when the model this turn went to answers in audio rather
   *  than in text. Mutually exclusive with `content` in practice -- a speech
   *  reply has no tokens and a chat reply has no clip -- but kept as a
   *  separate field rather than a tagged union, because the turn's identity
   *  is the request and not the media type of what came back. */
  audio: SpeechResult | null
  /** The model this turn was SENT to, recorded at send time on both halves of
   *  the exchange. The answer also carries `meta.model` -- what actually
   *  served -- and that one wins where both exist; this is what a question has
   *  instead, and what an answer has before the first byte comes back. */
  model: string | null
}
