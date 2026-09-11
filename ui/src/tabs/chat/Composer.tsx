import { useState, type ChangeEvent, type KeyboardEvent } from 'react'
import type { SpeechFormat, VoiceLibrary } from '../../api/types'
import { OWN_VOICE, VoiceFields, resolveVoice } from './VoiceFields'
import { DEFAULT_LANGUAGE, UploadFields, type Upload } from './UploadFields'
import { RequestParamsFields, type ChatParams } from './RequestParams'

/** An image read into memory as a data URL -- both the wire payload
 *  (`image_url.url` accepts a data URL directly, same as OpenAI's own API)
 *  and the local preview source, so there is one copy of the bytes rather
 *  than a data URL for the request and a separate object URL for display. */
export interface ImageAttachment {
  name: string
  dataUrl: string
}

/** The gateway's own reference point for what a request body may carry
 *  (`control_plane/gateway/settings.py`: "25 MiB is what OpenAI accepts").
 *  Rejecting an oversized image here is cheaper than letting the gateway do
 *  it after the whole file has already gone over the wire. */
const MAX_IMAGE_BYTES = 25 * 1024 * 1024

/** What a send carries beyond the text, when the endpoint takes more than
 *  text.
 *
 *  A discriminated union rather than optional fields, because the three
 *  endpoints take disjoint inputs and `ChatTab` switches on exactly this: one
 *  value decides which controls are drawn AND which URL the send goes to, so
 *  the composer cannot offer a voice for a request that has no voice field. */
export type SendExtra =
  | { kind: 'chat'; images: ImageAttachment[] }
  | { kind: 'speech'; voice: string | undefined; format: SpeechFormat }
  | { kind: 'transcription'; file: File; language: string | undefined }

interface Props {
  /** No model selected, or the one selected cannot serve. */
  disabled: boolean
  busy: boolean
  onSend: (text: string, extra: SendExtra) => void
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
  params: ChatParams
  onParamsChange: (next: ChatParams) => void
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
  params,
  onParamsChange,
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
  const [images, setImages] = useState<ImageAttachment[]>([])
  const [attachError, setAttachError] = useState<string | null>(null)

  const onAttach = (e: ChangeEvent<HTMLInputElement>) => {
    const files = Array.from(e.target.files ?? [])
    e.target.value = ''
    if (files.length === 0) return
    const oversized = files.find((f) => f.size > MAX_IMAGE_BYTES)
    if (oversized) {
      setAttachError(`${oversized.name} is over the 25 MiB a request body here accepts.`)
      return
    }
    setAttachError(null)
    files.forEach((file) => {
      const reader = new FileReader()
      reader.onload = () => {
        if (typeof reader.result === 'string') {
          setImages((prev) => [...prev, { name: file.name, dataUrl: reader.result as string }])
        }
      }
      reader.readAsDataURL(file)
    })
  }
  const removeImage = (name: string) =>
    setImages((prev) => prev.filter((img) => img.name !== name))

  /** What the button sends, or null when it cannot send yet. One function,
   *  so the disabled state and the payload cannot disagree about whether this
   *  composer is ready -- they were two conditions and the file case made
   *  that a bug waiting to happen. */
  const ready = (): { text: string; extra: SendExtra } | null => {
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
    if (speaking) {
      if (!body) return null
      const active = resolveVoice(voice, library)
      return {
        text: body,
        extra: { kind: 'speech', voice: active === OWN_VOICE ? undefined : active, format },
      }
    }
    // An image with no caption is still a real request -- "describe this" is
    // the image talking, not the empty textarea.
    if (!body && images.length === 0) return null
    return { text: body, extra: { kind: 'chat', images } }
  }

  const send = () => {
    const next = ready()
    if (!next) return
    if (!transcribing) setText('')
    if (next.extra.kind === 'chat') setImages([])
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
          {/* Sampling controls and an image attach are both only meaningful
              on the plain chat endpoint -- a speech request has no `messages`
              array for a system prompt to join and no vision input for an
              audio model to read, so both are gated on `!speaking` too, not
              only on `!transcribing`. */}
          {!speaking ? (
            <>
              <RequestParamsFields value={params} onChange={onParamsChange} disabled={disabled} />

              <div className="attachrow">
                <label className="filepick">
                  <span className="unit">Attach image</span>
                  <input type="file" accept="image/*" multiple onChange={onAttach} />
                </label>
                <span className="unit">
                  No model here advertises vision support — this sends anyway
                  and a model that cannot read it will refuse the request.
                </span>
              </div>
              {attachError ? (
                <p className="unit" style={{ color: 'var(--warn)' }}>
                  {attachError}
                </p>
              ) : null}
              {images.length > 0 ? (
                <div className="attachchips">
                  {images.map((img) => (
                    <span key={img.name} className="attachchip">
                      <img src={img.dataUrl} alt={img.name} />
                      {img.name}
                      <button
                        type="button"
                        onClick={() => removeImage(img.name)}
                        aria-label={`Remove ${img.name}`}
                      >
                        ×
                      </button>
                    </span>
                  ))}
                </div>
              ) : null}
            </>
          ) : null}

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
