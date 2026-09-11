import { SPEECH_FORMATS, type SpeechFormat, type VoiceLibrary } from '../../api/types'
import { Select, type SelectOption } from '../../components/Select'
import { Verbatim, VerbatimList } from '../../components/Verbatim'

/** The value of the "no voice" option.
 *
 *  Empty string rather than a sentinel word, because it is what an unset
 *  dropdown carries anyway and because the field it maps to is *absent*
 *  from the request body, not set to something. Naming no voice is a real
 *  request: the model speaks in its own. */
export const OWN_VOICE = ''

/** A voice that was installed when the page loaded and is not there now would
 *  otherwise stay selected and be refused on send. Both call sites fall back
 *  the way the model picker does rather than holding a stale name, so the rule
 *  lives here with the field it applies to. */
export function resolveVoice(voice: string, library: VoiceLibrary | null): string {
  const voices = library?.voices ?? []
  return voice !== OWN_VOICE && voices.includes(voice) ? voice : OWN_VOICE
}

interface Props {
  disabled: boolean
  library: VoiceLibrary | null
  /** Why the voice list is unavailable, in the server's own words. Not fatal:
   *  a request with no voice named still works, so this is reported beside a
   *  usable form rather than in place of one. */
  voicesError: string | null
  voice: string
  format: SpeechFormat
  onVoice: (v: string) => void
  onFormat: (f: SpeechFormat) => void
  /** Namespaces the voice/format dropdown pair against the rest of the composer. */
  idPrefix: string
}

/** Voice and format, plus the server's own words about the library.
 *
 *  Drawn in the Chat composer whenever the picked model answers in audio.
 *  `voices.check.mjs` compares `SPEECH_FORMATS` against the runtime's own
 *  `FORMATS` table, so the dropdown can't offer a format the server refuses. */
export function VoiceFields({
  disabled,
  library,
  voicesError,
  voice,
  format,
  onVoice,
  onFormat,
  idPrefix,
}: Props) {
  const voices = library?.voices ?? []
  // First and default. A cloned voice needs a reference clip AND that clip's
  // exact transcript installed on the node; the model's own voice needs
  // nothing, so it is the option that always works.
  const voiceOptions: SelectOption<string>[] = [
    { value: OWN_VOICE, label: 'the model’s own voice' },
    ...voices.map((v) => ({ value: v, label: v })),
  ]
  const formatOptions: SelectOption<SpeechFormat>[] = SPEECH_FORMATS.map((f) => ({
    value: f,
    label: f,
  }))
  return (
    <>
      <div className="speechrow">
        <div className="fld">
          <label htmlFor={`${idPrefix}-voice`}>Voice</label>
          <Select
            id={`${idPrefix}-voice`}
            value={resolveVoice(voice, library)}
            disabled={disabled}
            options={voiceOptions}
            onChange={onVoice}
          />
        </div>

        <div className="fld">
          <label htmlFor={`${idPrefix}-format`}>Format</label>
          <Select
            id={`${idPrefix}-format`}
            value={format}
            disabled={disabled}
            options={formatOptions}
            onChange={onFormat}
          />
        </div>

        <span style={{ flex: 1 }} />
      </div>

      {/* Both of these are the server explaining itself, so both go through
          Verbatim. `skipped` is the only thing that tells somebody why a clip
          they installed is not in the list above. */}
      {voicesError ? <Verbatim text={voicesError} size="unit" /> : null}
      {library && library.skipped.length > 0 ? (
        <VerbatimList items={library.skipped} />
      ) : null}
    </>
  )
}
