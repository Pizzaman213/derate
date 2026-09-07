/** A refusal the operator is allowed to overrule, and the sentence they have
 *  to read first.
 *
 *  Two surfaces refuse a launch on live memory and both must offer the same
 *  way through: the dashboard's dry run, which learns about the refusal from
 *  `serve.override_required` before anything is sent, and the quantization
 *  ladder, which learns about it from a 409 `live_memory_insufficient` after
 *  trying. They arrive at the gate from opposite directions, so this component
 *  takes no verdict and no plan -- only the two strings and the callbacks --
 *  and each caller builds the sentence from what it actually knows.
 *
 *  The checkbox label IS the claim rather than a bare "I understand": a box
 *  whose text does not name the measurement it is waiving can be ticked
 *  without reading it, which is the failure this gate exists to prevent. The
 *  button does not appear until it is ticked, so the override is always a
 *  second, deliberate act.
 *
 *  `onLaunch` is optional because a launch can need more than one permission
 *  at once. When several gates are stacked, each renders its own claim and its
 *  own checkbox but none renders a button, and the caller puts one button
 *  below the stack that appears only when every box is ticked -- one launch,
 *  one button, however many permissions it took. */
export function OverrideGate({
  reason,
  sentence,
  checked,
  onChange,
  onLaunch,
  launching,
  launchLabel = 'Serve anyway',
}: {
  /** The gate's own refusal, verbatim. Never paraphrased -- it names the
   *  numbers it used, and a rewrite would drop them. */
  reason: string
  /** What ticking the box means, in the first person, naming the measurement
   *  being overridden. */
  sentence: string
  checked: boolean
  onChange: (v: boolean) => void
  /** Omit when this gate is one of several: the caller owns the button. */
  onLaunch?: () => void
  launching?: boolean
  launchLabel?: string
}) {
  return (
    <div style={{ display: 'grid', gap: 8 }}>
      <p
        className="label"
        style={{ margin: 0, fontWeight: 400, color: 'var(--fault)', whiteSpace: 'pre-wrap' }}
      >
        {reason}
      </p>
      <label style={{ display: 'flex', gap: 8, alignItems: 'flex-start', cursor: 'pointer' }}>
        <input
          type="checkbox"
          checked={checked}
          onChange={(e) => onChange(e.target.checked)}
          style={{ marginTop: 3 }}
        />
        <span className="label" style={{ fontWeight: 400 }}>
          {sentence}
        </span>
      </label>
      {checked && onLaunch ? (
        <div>
          <button onClick={onLaunch} disabled={launching}>
            {launching ? 'Launching…' : launchLabel}
          </button>
        </div>
      ) : null}
    </div>
  )
}
