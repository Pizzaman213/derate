import { useState, type KeyboardEvent } from 'react'
import type { SpeechFormat, VoiceLibrary } from '../../api/types'
import { OWN_VOICE, VoiceFields, resolveVoice } from './VoiceFields'
import { DEFAULT_LANGUAGE, UploadFields, type Upload } from './UploadFields'

/** What a send carries beyond the text, when the endpoint takes more than
 *  text. `null` is a chat completion.
 *
 *  A discriminated union rather than three optional fields, because the three
 *  endpoints take disjoint inputs and `ChatTab` switches on exactly this: one
 *  value decides which controls are drawn AND which URL the send goes to, so
 *  the composer cannot offer a voice for a request that has no voice field. */
export type SendExtra =
  | { kind: 'speech'; voice: string | undefined; format: SpeechFormat }
  | { kind: 'transcription'; file: File; language: string | undefined }

interface Props {
  /** No model selected, or the one selected cannot serve. */
  disabled: boolean
  busy: boolean
  onSend: (text: string, extra: SendExtra | null) => void
  onStop: () => void
  onClear: () => void
  canClear: boolean
  /** The picked model answers in audio. Draws the voice and format fields and
   *  makes `onSend` carry them. */
  speaking?: boolean
  library?: VoiceLibrary | null
  voicesError?: string | null
  /** The picked model takes audio and answers in text. Draws a file chooser
   *  and a language field in place of the message box -- there is no message
   *  on this endpoint, and a textarea above a file input would be a control
   *  whose contents are silently discarded. */
  transcribing?: boolean
}

export function Composer({
  disabled,
  busy,
  onSend,
  onStop,
  onClear,
  canClear,
  speaking = false,
  library = null,
  voicesError = null,
  transcribing = false,
}: Props) {
  const [text, setText] = useState('')
  // Held across a switch to a text model and back on purpose: picking a voice,
  // trying a chat model, and coming back should not silently reset it. It is
  // only ever READ when `speaking`, and `resolveVoice` drops a name the
  // current model does not have -- so a stale voice cannot be sent to a
  // deployment that would refuse it.
  const [voice, setVoice] = useState<string>(OWN_VOICE)
  const [format, setFormat] = useState<SpeechFormat>('mp3')
  const [upload, setUpload] = useState<Upload>({ file: null, language: DEFAULT_LANGUAGE })

  /** What the button sends, or null when it cannot send yet. One function,
   *  so the disabled state and the payload cannot disagree about whether this
   *  composer is ready -- they were two conditions and the file case made
   *  that a bug waiting to happen. */
  const ready = (): { text: string; extra: SendExtra | null } | null => {
    if (disabled || busy) return null
    if (transcribing) {
      // The file IS the request. There is no text on this endpoint, so an
      // empty message box must not block it -- and the turn label is the
      // file's name rather than anything typed.
      if (!upload.file) return null
      return {
        text: upload.file.name,
        extra: {
          kind: 'transcription',
          file: upload.file,
          language: upload.language || undefined,
        },
      }
    }
    const body = text.trim()
    if (!body) return null
    if (!speaking) return { text: body, extra: null }
    const active = resolveVoice(voice, library)
    return {
      text: body,
      extra: { kind: 'speech', voice: active === OWN_VOICE ? undefined : active, format },
    }
  }

  const send = () => {
    const next = ready()
    if (!next) return
    if (!transcribing) setText('')
    onSend(next.text, next.extra)
  }

  const onKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    // Enter sends, Shift+Enter breaks the line. The composer is the only
    // multi-line input in the product, so it says so under the field rather
    // than assuming the convention is obvious.
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      send()
    }
  }

  return (
    <div className="composer">
      {/* Only for a model that answers in audio. A voice and a container are
          meaningless for a chat completion, and two dead selects above the
          composer would be the same kind of dead end that keeping speech
          models out of this picker used to be. */}
      {speaking ? (
        <VoiceFields
          disabled={disabled}
          library={library}
          voicesError={voicesError}
          voice={voice}
          format={format}
          onVoice={setVoice}
          onFormat={setFormat}
          idPrefix="chat"
        />
      ) : null}

      {transcribing ? (
        <UploadFields
          disabled={disabled}
          value={upload}
          onChange={setUpload}
          idPrefix="chat"
        />
      ) : (
        <>
          <label className="sr-only" htmlFor="chat-input">
            Message
          </label>
          <textarea
            id="chat-input"
            value={text}
            disabled={disabled}
            onChange={(e) => setText(e.target.value)}
            onKeyDown={onKeyDown}
            placeholder={
              disabled ? 'Pick a model first' : speaking ? 'Something to say' : 'Say something'
            }
            rows={3}
          />
        </>
      )}
      <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--s-2)' }}>
        <span className="unit">
          {transcribing
            ? 'The file is the request — there is no message on this endpoint'
            : `${speaking ? 'Enter speaks' : 'Enter sends'} · Shift+Enter for a new line`}
        </span>
        <span style={{ flex: 1 }} />
        <button type="button" onClick={onClear} disabled={!canClear || busy}>
          Clear
        </button>
        {busy ? (
          <button type="button" onClick={onStop}>
            Stop
          </button>
        ) : (
          <button type="button" onClick={send} disabled={ready() === null}>
            {transcribing ? 'Transcribe' : speaking ? 'Speak' : 'Send'}
          </button>
        )}
      </div>
    </div>
  )
}
