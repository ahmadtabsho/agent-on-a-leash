import { useCallback, useEffect, useState } from 'react'
import { api } from './api.js'

const LABEL = { approve: 'Approved', decline: 'Declined', step_up: 'Asked you' }

function money(chf) {
  return `CHF ${Number(chf).toFixed(2)}`
}

/** What the system understood, shown back before anything is authorised. */
function PolicyReview({ policy, mandate, onConfirm, busy }) {
  if (!policy) return null
  const active = mandate?.status === 'active'
  const revoked = mandate?.status === 'revoked'

  return (
    <div className="card">
      <h2>What we understood</h2>
      <p className="hint">
        These are the checks we will apply. Nothing is authorised until you confirm them.
      </p>

      <ul className="rules">
        {policy.hard_rules.map((rule, i) => (
          <li key={i}>
            <code>
              {rule.field} {rule.operator}{' '}
              {Array.isArray(rule.value) ? rule.value.join(', ') : String(rule.value)}
              {rule.period_days ? ` over any ${rule.period_days} days` : ''}
            </code>
            {rule.source ? <span className="src">from “{rule.source}”</span> : null}
          </li>
        ))}
      </ul>

      {policy.guidance.length > 0 && (
        <div className="block">
          <h3>In plain words</h3>
          <ul>{policy.guidance.map((line, i) => <li key={i}>{line}</li>)}</ul>
        </div>
      )}

      {policy.open_questions.length > 0 && (
        <div className="block questions">
          <h3>We need you to decide</h3>
          <ul>{policy.open_questions.map((q, i) => <li key={i}>{q}</li>)}</ul>
        </div>
      )}

      <div className="block">
        <h3>When we cannot settle a purchase</h3>
        <p className="empty">
          {policy.uncertainty_policy === 'ask'
            ? 'We bring it to you rather than guessing.'
            : policy.uncertainty_policy === 'decline'
              ? 'We refuse it.'
              : 'We allow it, as you asked.'}
        </p>
      </div>

      <div className="row">
        {!active && !revoked && (
          <button className="primary" onClick={onConfirm} disabled={busy}>
            These are right — authorise them
          </button>
        )}
        {active && <span className="stamp">Authorised {mandate.confirmed_at?.slice(11, 19)} UTC</span>}
        {revoked && <span className="stamp">Permission withdrawn — nothing can be spent.</span>}
      </div>
    </div>
  )
}

/** One decision, with the evidence that produced it. */
function Step({ step }) {
  const adverse = step.evidence.filter((e) => e.outcome !== 'pass')
  return (
    <li className="step">
      <div className="step-head">
        <span className={`badge ${step.decision}`}>{LABEL[step.decision]}</span>
        <span className="who">{step.merchant_name}</span>
        <span>{money(step.billing_amount_chf)}</span>
        {step.resolved_by_customer && (
          <span className="tag">you {step.resolved_by_customer}d this</span>
        )}
        <span className="meta">
          {step.source_authorization_id} · {step.elapsed_ms.toFixed(2)} ms
        </span>
      </div>
      <p className="why">{step.customer_message}</p>
      {adverse.length > 0 && (
        <details>
          <summary>{adverse.length} finding{adverse.length > 1 ? 's' : ''}</summary>
          <ul className="evidence">
            {adverse.map((e, i) => (
              <li key={i}>
                <span className={`tag ${e.outcome}`}>{e.code}</span>
                <span>{e.detail}</span>
              </li>
            ))}
          </ul>
        </details>
      )}
    </li>
  )
}

/** Seconds remaining, ticking, so the countdown is honest rather than stale. */
function useCountdown(pending) {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    if (pending.length === 0) return undefined
    const id = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(id)
  }, [pending.length])
  return (expiresAt) => {
    if (!expiresAt) return null
    return Math.max(0, Math.round((new Date(expiresAt).getTime() - now) / 1000))
  }
}

/** Purchases paused for the customer. Only they can answer, and only in time. */
function Inbox({ pending, lapsed, onResolve, busy }) {
  const secondsLeft = useCountdown(pending)

  if (pending.length === 0 && lapsed.length === 0) {
    return (
      <div className="card">
        <h2>Waiting on you</h2>
        <p className="empty">Nothing needs your attention.</p>
      </div>
    )
  }
  return (
    <div className="card">
      <h2>Waiting on you</h2>
      {pending.length > 0 && (
        <p className="hint">
          These purchases are paused. They are not approved, and they do not count against your
          limits until you answer.
        </p>
      )}
      {pending.map((item) => {
        const left = secondsLeft(item.expires_at)
        return (
        <div className="inbox-item" key={item.authorization_id}>
          <div className="step-head">
            <span className="who">{item.merchant_name}</span>
            <span className="amount">{money(item.billing_amount_chf)}</span>
            {left !== null && (
              <span className={left <= 30 ? 'clock urgent' : 'clock'}>
                {left > 0 ? `${left}s to answer` : 'window closed'}
              </span>
            )}
            <span className="meta">{item.source_authorization_id}</span>
          </div>
          <p className="why">{item.customer_message}</p>
          <ul className="lines">
            {item.items.map((line) => (
              <li key={line.line_no}>
                {line.quantity} × {line.name} — {line.category}
              </li>
            ))}
          </ul>
          <div className="row">
            <button
              className="ok"
              disabled={busy}
              onClick={() => onResolve(item.authorization_id, 'approve')}
            >
              Yes, buy it
            </button>
            <button
              className="stop"
              disabled={busy}
              onClick={() => onResolve(item.authorization_id, 'decline')}
            >
              No, stop it
            </button>
          </div>
        </div>
        )
      })}

      {lapsed.length > 0 && (
        <div className="block">
          <h3>Asked, but not answered in time</h3>
          <p className="empty">
            Nothing was decided on your behalf. These were never approved, and they never
            counted against your limits.
          </p>
          {lapsed.map((item) => (
            <div className="lapsed-item" key={item.authorization_id}>
              <span className="who">{item.merchant_name}</span>{' '}
              <span>{money(item.billing_amount_chf)}</span>{' '}
              <span className="meta">{item.source_authorization_id}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

export default function App() {
  const [health, setHealth] = useState(null)
  const [instruction, setInstruction] = useState('')
  const [policy, setPolicy] = useState(null)
  const [mandate, setMandate] = useState(null)
  const [run, setRun] = useState(null)
  const [pending, setPending] = useState([])
  const [lapsed, setLapsed] = useState([])
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  const guard = useCallback(async (work) => {
    setBusy(true)
    setError('')
    try {
      return await work()
    } catch (exc) {
      setError(exc.message)
      return null
    } finally {
      setBusy(false)
    }
  }, [])

  useEffect(() => {
    api.health().then(setHealth).catch((e) => setError(e.message))
  }, [])

  // Re-read the inbox while anything is waiting, so a window that closes on
  // the server is reflected here rather than leaving a dead button.
  useEffect(() => {
    if (pending.length === 0) return undefined
    const id = setInterval(() => {
      api.pending().then((d) => {
        setPending(d.pending)
        setLapsed(d.lapsed || [])
      }).catch(() => {})
    }, 5000)
    return () => clearInterval(id)
  }, [pending.length])

  const refreshPending = useCallback(async () => {
    const data = await api.pending()
    setPending(data.pending)
    setLapsed(data.lapsed || [])
  }, [])

  const useScenario = (scenario) => {
    setInstruction(scenario.instruction)
    setPolicy(null)
    setRun(null)
  }

  const draft = () =>
    guard(async () => {
      const data = await api.draft(instruction)
      setPolicy(data.policy)
      setMandate(data.mandate)
      setRun(null)
    })

  const confirm = () =>
    guard(async () => {
      const data = await api.confirm()
      setMandate(data.mandate)
    })

  const start = (scenarioId) =>
    guard(async () => {
      const data = await api.run(scenarioId)
      setRun(data)
      await refreshPending()
    })

  const resolve = (authorizationId, decision) =>
    guard(async () => {
      await api.resolve(authorizationId, decision)
      await refreshPending()
      if (run) setRun(await api.run(run.scenario_id).catch(() => run))
    })

  const tighten = () =>
    guard(async () => {
      const data = await api.tighten({ uncertainty_policy: 'decline' })
      setMandate(data.mandate)
    })

  const revoke = () =>
    guard(async () => {
      const data = await api.revoke()
      setMandate(data.mandate)
      setRun(null)
    })

  const reset = () =>
    guard(async () => {
      await api.reset()
      setPolicy(null)
      setMandate(null)
      setRun(null)
      setPending([])
      setLapsed([])
      setInstruction('')
    })

  const active = mandate?.status === 'active'

  return (
    <div className="shell">
      <header className="masthead">
        <h1>Wallet control</h1>
        <p>Decide what an AI shopping agent may spend on your card — and take it back at any time.</p>
        {health && (
          <span className="mode">
            <span className="dot" />
            {health.mode === 'sandbox' ? 'Connected to the sandbox' : 'Offline scenarios'} ·{' '}
            {health.engine_version}
            {health.advisor_enabled ? ' · second opinion on' : ''}
          </span>
        )}
      </header>

      {error && <div className="error">{error}</div>}

      <div className="card">
        <h2>What may the agent buy?</h2>
        <p className="hint">Write it however you would say it. We turn it into checks you can see.</p>
        <textarea
          value={instruction}
          onChange={(e) => setInstruction(e.target.value)}
          placeholder="Buy me black running shoes for up to CHF 200. Ask me when uncertain."
        />
        <div className="row">
          <button className="primary" onClick={draft} disabled={busy || !instruction.trim()}>
            Show me what that means
          </button>
          {active && (
            <>
              <button onClick={tighten} disabled={busy}>Tighten: refuse when unsure</button>
              <button className="stop" onClick={revoke} disabled={busy}>Withdraw permission</button>
            </>
          )}
          <button onClick={reset} disabled={busy}>Start over</button>
        </div>

        {health && (
          <div className="block">
            <h3>Or start from a supplied scenario</h3>
            <div className="scenarios">
              {health.scenarios.map((s) => (
                <button className="scenario" key={s.scenario_id} onClick={() => useScenario(s)}>
                  <b>{s.name}</b>
                  <small>{s.events} purchase{s.events > 1 ? 's' : ''} · {s.scenario_id}</small>
                </button>
              ))}
            </div>
          </div>
        )}
      </div>

      <PolicyReview policy={policy} mandate={mandate} onConfirm={confirm} busy={busy} />

      {active && health && (
        <div className="card">
          <h2>Let the agent shop</h2>
          <p className="hint">Each purchase is judged against the policy you authorised.</p>
          <div className="scenarios">
            {health.scenarios.map((s) => (
              <button className="scenario" key={s.scenario_id} onClick={() => start(s.scenario_id)} disabled={busy}>
                <b>{s.name}</b>
                <small>{s.events} purchase{s.events > 1 ? 's' : ''}</small>
              </button>
            ))}
          </div>
        </div>
      )}

      <Inbox pending={pending} lapsed={lapsed} onResolve={resolve} busy={busy} />

      {run && (
        <div className="card">
          <h2>{run.name}</h2>
          <p className="hint">Judged against the policy you authorised: “{run.instruction}”</p>
          {run.policy_matches_scenario === false && (
            <div className="mismatch">
              <strong>These purchases are not what your policy is about.</strong> The agent in
              this scenario was told: “{run.scenario_instruction}” — so a lot will be refused
              simply for falling outside what you authorised. That is correct, but to see the
              scenario properly, authorise its own instruction instead.
            </div>
          )}
          <ul className="steps">
            {run.steps.map((step) => (
              <Step key={step.authorization_id} step={step} />
            ))}
          </ul>
          <div className="tally">
            <span>{run.counts.approve} approved</span>
            <span>{run.counts.decline} declined</span>
            <span>{run.counts.step_up} brought to you</span>
          </div>
        </div>
      )}
    </div>
  )
}
