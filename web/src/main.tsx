import { useCallback, useEffect, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import { briefLength, labels, money, terminal } from "./state";
import "./style.css";
import { serialPoll } from "./polling";

type Artifact = { action_id: string; path: string; sha256: string };
type EconomySummary = {
  totals?: {
    before_tokens: number;
    after_tokens: number;
    cache_hit: number;
    provider_cached_tokens: number;
    measurements: number;
  };
};
type Report = {
  summary: string;
  artifacts: Artifact[];
  remaining?: string[];
  verification?: { reason: string };
  uncertain_actions: string[];
  pending_actions?: string[];
  reserved_usd?: number;
};
type Job = {
  id: string;
  status: string;
  created: number;
  deadline: number;
  spent: number;
  reserved: number;
  budget: number;
  step: number;
  prompt?: string;
  plan?: { goals: { id: string; description: string; check: string }[] };
  report?: Report;
};
type Event = {
  seq: number;
  kind: string;
  created: number;
  data: Record<string, unknown>;
};

function App() {
  const [token, setToken] = useState(""),
    [entry, setEntry] = useState(""),
    [connected, setConnected] = useState(false);
  const [jobs, setJobs] = useState<Job[]>([]),
    [selected, setSelected] = useState(""),
    [job, setJob] = useState<Job | null>(null);
  const [prompt, setPrompt] = useState(""),
    [busy, setBusy] = useState(false),
    [error, setError] = useState("");
  const [events, setEvents] = useState<Event[]>([]),
    [worker, setWorker] = useState(false),
    [screen, setScreen] = useState("");
  const [economy, setEconomy] = useState<EconomySummary | null>(null);
  const screenRef = useRef("");
  const cursorRef = useRef(0);
  const selectedRef = useRef(selected);
  selectedRef.current = selected;
  const revisionRef = useRef(0);
  const authRef = useRef(0);
  const submissionRef = useRef<{ brief: string; key: string } | null>(null);
  const [connecting, setConnecting] = useState(false);
  const api = useCallback(
    async (path: string, init: RequestInit = {}) => {
      const response = await fetch("/api" + path, {
        ...init,
        headers: {
          "Content-Type": "application/json",
          Authorization: "Bearer " + token,
          ...init.headers,
        },
        cache: "no-store",
      });
      if (!response.ok) {
        const data = await response
          .json()
          .catch(() => ({ detail: response.statusText }));
        throw new Error(
          typeof data.detail === "string"
            ? data.detail
            : JSON.stringify(data.detail),
        );
      }
      return response;
    },
    [token],
  );

  useEffect(() => {
    if (!connected) return;
    return serialPoll(
      async (signal) => {
        const [a, b] = await Promise.all([
          api("/jobs", { signal }),
          api("/health", { signal }),
        ]);
        const [listed, health] = await Promise.all([a.json(), b.json()]);
        if (signal.aborted) return;
        setJobs(listed);
        setWorker(health.worker_online);
      },
      2500,
      (e) => setError(String(e)),
    );
  }, [connected, api]);

  useEffect(() => {
    setEvents([]);
    cursorRef.current = 0;
    setJob(null);
    setEconomy(null);
    setScreen("");
    if (screenRef.current) {
      URL.revokeObjectURL(screenRef.current);
      screenRef.current = "";
    }
    if (!selected || !connected) return;
    return serialPoll(
      async (signal) => {
        const revision = revisionRef.current;
        const [a, b, c] = await Promise.all([
          api("/jobs/" + selected, { signal }),
          api(
            "/jobs/" +
              selected +
              "/events?summary=true&after=" +
              cursorRef.current,
            { signal },
          ),
          api("/jobs/" + selected + "/economy", { signal }),
        ]);
        const detail: Job = await a.json();
        const log: Event[] = await b.json();
        const costs: EconomySummary = await c.json();
        if (signal.aborted || revision !== revisionRef.current) return;
        setJob(detail);
        setEconomy(costs);
        if (log.length) {
          cursorRef.current = Math.max(
            cursorRef.current,
            log[log.length - 1].seq,
          );
          setEvents((old) =>
            Array.from(
              new Map([...old, ...log].map((e) => [e.seq, e])).values(),
            ).slice(-100),
          );
        }
        const image = await fetch("/api/jobs/" + selected + "/screen", {
          headers: { Authorization: "Bearer " + token },
          cache: "no-store",
          signal,
        });
        if (!image.ok) return;
        const blob = await image.blob();
        if (signal.aborted || revision !== revisionRef.current) return;
        const next = URL.createObjectURL(blob);
        URL.revokeObjectURL(screenRef.current);
        screenRef.current = next;
        setScreen(next);
      },
      2500,
      (e) => setError(String(e)),
    );
  }, [selected, connected, api, token]);

  async function connect() {
    setConnecting(true);
    setError("");
    try {
      await api("/health");
      authRef.current += 1;
      setConnected(true);
    } catch (e) {
      setError(String(e));
    } finally {
      setConnecting(false);
    }
  }
  async function submit() {
    setBusy(true);
    setError("");
    const auth = authRef.current;
    if (
      !submissionRef.current ||
      submissionRef.current.brief !== prompt.trim()
    ) {
      submissionRef.current = {
        brief: prompt.trim(),
        key: crypto.randomUUID(),
      };
    }
    try {
      const response = await api("/jobs", {
        headers: { "Idempotency-Key": submissionRef.current.key },
        method: "POST",
        body: JSON.stringify({ prompt, budget_usd: 5, max_seconds: 86400 }),
      });
      const created: Job = await response.json();
      if (auth !== authRef.current) return;
      submissionRef.current = null;
      setSelected(created.id);
      setJobs((old) => [created, ...old]);
      setPrompt("");
    } catch (e) {
      if (auth === authRef.current) setError(String(e));
    } finally {
      if (auth === authRef.current) setBusy(false);
    }
  }
  async function cancel() {
    if (!job) return;
    revisionRef.current += 1;
    try {
      const updated: Job = await (
        await api("/jobs/" + job.id + "/cancel", { method: "POST" })
      ).json();
      if (selectedRef.current === updated.id) setJob(updated);
    } catch (e) {
      setError(String(e));
    }
  }
  async function download(a: Artifact) {
    if (!job) return;
    try {
      const r = await api("/jobs/" + job.id + "/artifacts/" + a.action_id);
      const url = URL.createObjectURL(await r.blob());
      const link = document.createElement("a");
      link.href = url;
      link.download = a.path.split("/").pop() || "artifact";
      link.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (e) {
      setError(String(e));
    }
  }

  if (!connected)
    return (
      <main className="entry">
        <div className="mark">
          EA<span>01</span>
        </div>
        <p className="eyebrow">AUTONOMOUS WORKSPACE</p>
        <h1>
          Bir tapşırıq.
          <br />
          Ardıcıl icra.
        </h1>
        <p className="muted">
          Brauzer, Windows və kod işlərini ayrıca mühitdə idarə et.
        </p>
        <label htmlFor="token">İdarəetmə açarı</label>
        <input
          id="token"
          disabled={connecting}
          type="password"
          autoComplete="off"
          value={entry}
          onChange={(e) => {
            setEntry(e.target.value);
            setToken(e.target.value);
          }}
          placeholder="EA_API_TOKEN"
          onKeyDown={(e) => {
            if (e.key === "Enter") void connect();
          }}
        />
        <button disabled={connecting} onClick={() => void connect()}>
          {connecting ? "Qoşulur…" : "Panelə daxil ol →"}
        </button>
        <small>Açar yalnız bu səhifənin yaddaşında qalır.</small>
        {error && (
          <p role="alert" className="error">
            {error}
          </p>
        )}
      </main>
    );

  return (
    <div className="shell">
      <aside>
        <a className="brand" href="/">
          EXTREME<span>AUTOPILOT</span>
        </a>
        <div className={"connection " + (worker ? "online" : "")}>
          <i />
          {worker ? "Worker qoşulub" : "Worker gözlənilir"}
        </div>
        <h2>
          Tapşırıqlar <span>{jobs.length}</span>
        </h2>
        <button className="secondary" onClick={() => setSelected("")}>
          ＋ Yeni tapşırıq
        </button>
        <nav>
          {jobs.map((j) => (
            <button
              className={"job-link " + (j.id === selected ? "chosen" : "")}
              key={j.id}
              onClick={() => setSelected(j.id)}
            >
              <span className={"dot " + j.status} />
              <div>
                <b>{j.id.slice(0, 8)}</b>
                <small>{labels[j.status] || j.status}</small>
              </div>
              <span>{money(j.spent)}</span>
            </button>
          ))}
        </nav>
        <footer>
          İcra mühiti: ayrıca Windows
          <br />
          Bir worker · ardıcıl növbə
          <button
            className="text"
            onClick={() => {
              authRef.current += 1;
              revisionRef.current += 1;
              setConnected(false);
              setSelected("");
              setJobs([]);
              setJob(null);
              setPrompt("");
              setBusy(false);
              submissionRef.current = null;
              setError("");
              setToken("");
              setEntry("");
            }}
          >
            Çıxış
          </button>
        </footer>
      </aside>
      <main className="workspace">
        <header>
          <div>
            <p className="eyebrow">EXTREME AUTOPILOT / İDARƏETMƏ</p>
            <h1>{selected ? "İcra mərkəzi" : "Nəyi tamamlayırıq?"}</h1>
          </div>
          <span className="badge">24 saat / $5 bulud</span>
        </header>
        {error && (
          <div role="alert" className="error">
            {error}
            <button className="text" onClick={() => setError("")}>
              Bağla
            </button>
          </div>
        )}
        {!selected ? (
          <section className="compose">
            <h2>Məqsədi bir dəfə yaz.</h2>
            <p className="muted">
              Gözlənilən nəticəni, mənbələri, faylları və icazə verdiyin
              əməliyyatları göstər. Xaricə göndərmə və ya dərc lazımdırsa, dəqiq
              ünvanı da yaz.
            </p>
            <label htmlFor="brief">Tapşırığın mətni</label>
            <textarea
              id="brief"
              value={prompt}
              maxLength={50000}
              onChange={(e) => setPrompt(e.target.value)}
              placeholder="Məqsəd, işlənəcək saytlar/proqramlar, nəticə faylları və yoxlama meyarları…"
            />
            <div className="compose-bottom">
              <span className={briefLength(prompt) >= 200 ? "good" : "muted"}>
                {briefLength(prompt)} / minimum 200 simvol
              </span>
              <button
                disabled={busy || briefLength(prompt) < 200}
                onClick={() => void submit()}
              >
                {busy ? "Növbəyə əlavə edilir…" : "Tapşırığı başlat →"}
              </button>
            </div>
            <div className="note">
              İcra zamanı əlavə sual verilmir. Giriş problemi və ya başqa maneə
              olarsa, görülən və qalan işlər hesabatda göstərilir.
            </div>
          </section>
        ) : job ? (
          <>
            <div className="metrics">
              <article>
                <small>Vəziyyət</small>
                <strong>{labels[job.status]}</strong>
              </article>
              <article>
                <small>Bulud istifadəsi</small>
                <strong>
                  {money(job.spent)} <em>/ {money(job.budget)}</em>
                </strong>
                <progress value={job.spent + job.reserved} max={job.budget} />
              </article>
              <article>
                <small>Əməliyyat sayı</small>
                <strong>{job.step}</strong>
              </article>
              <article>
                <small>Son müddət</small>
                <strong className="date">
                  {new Date(job.deadline * 1000).toLocaleString("az-AZ")}
                </strong>
              </article>
            </div>
            <div className="columns">
              <section className="card">
                <div className="section-title">
                  <h2>İcra planı</h2>
                  {!terminal.has(job.status) && (
                    <button className="danger" onClick={() => void cancel()}>
                      İcranı dayandır
                    </button>
                  )}
                </div>
                {job.plan ? (
                  <ol className="goals">
                    {job.plan.goals.map((g) => (
                      <li key={g.id}>
                        <b>{g.description}</b>
                        <p>{g.check}</p>
                      </li>
                    ))}
                  </ol>
                ) : (
                  <p className="muted">
                    Worker və model hazır olduqda plan yaranacaq.
                  </p>
                )}
                <details>
                  <summary>İlkin tapşırıq</summary>
                  <p className="pre">{job.prompt}</p>
                </details>
              </section>
              <section className="card">
                <h2>Son müşahidə</h2>
                {screen ? (
                  <img
                    className="screen"
                    src={screen}
                    alt="Worker mühitindən son ekran müşahidəsi"
                  />
                ) : (
                  <div className="empty-screen">
                    Ekran görüntüsü hələ toplanmayıb
                  </div>
                )}
                <small className="muted">
                  Bu, son saxlanmış müşahidədir; fasiləsiz video deyil.
                </small>
              </section>
            </div>
            {economy?.totals && (
              <section className="card">
                <h2>Token qənaəti</h2>
                <p>
                  Ölçülmüş mətn konteksti:{" "}
                  {economy.totals.before_tokens.toLocaleString()} →{" "}
                  {economy.totals.after_tokens.toLocaleString()} təxmini token.
                </p>
                <p>
                  Plan keşindən istifadə: {economy.totals.cache_hit}.
                  Provayderin bildirdiyi keş tokenləri:{" "}
                  {economy.totals.provider_cached_tokens.toLocaleString()}.
                </p>
                <small className="muted">
                  Son 100 ölçmə. Dollar qənaəti zəmanəti deyil; sayğacın əsası
                  tokenizer sazlamasından asılıdır.
                </small>
              </section>
            )}
            {job.report && (
              <section className="card report">
                <p className="eyebrow">YEKUN HESABAT</p>
                <h2>{labels[job.status]}</h2>
                <p className="pre">{job.report.summary}</p>
                {job.report.verification && (
                  <p className="good">
                    Yoxlama: {job.report.verification.reason}
                  </p>
                )}
                {job.report.remaining?.length ? (
                  <>
                    <h3>Qalan işlər</h3>
                    <ul>
                      {job.report.remaining.map((r, i) => (
                        <li key={i}>{r}</li>
                      ))}
                    </ul>
                  </>
                ) : null}
                {job.report.artifacts?.map((a) => (
                  <button
                    className="artifact"
                    key={a.action_id}
                    onClick={() => void download(a)}
                  >
                    ↓ {a.path}
                    <small>{a.sha256.slice(0, 16)}</small>
                  </button>
                ))}
                {!!job.report.pending_actions?.length && (
                  <p className="error">
                    Hələ nəticəsi gəlməyən əməliyyatlar:{" "}
                    {job.report.pending_actions.length}
                  </p>
                )}
                {!!job.report.reserved_usd && (
                  <p className="muted">
                    Gözlənilən bulud hesablaşması:{" "}
                    {money(job.report.reserved_usd)}
                  </p>
                )}
                {job.report.uncertain_actions?.length > 0 && (
                  <p className="error">
                    Nəticəsi qeyri-müəyyən əməliyyatlar:{" "}
                    {job.report.uncertain_actions.join(", ")}
                  </p>
                )}
              </section>
            )}
            <section className="card">
              <h2>Əməliyyat jurnalı</h2>
              <div className="timeline">
                {events.map((e) => (
                  <div key={e.seq}>
                    <time>
                      {new Date(e.created * 1000).toLocaleTimeString("az-AZ")}
                    </time>
                    <b>{e.kind}</b>
                    <span>
                      {String(
                        e.data.tool || e.data.message || e.data.error || "",
                      )}
                    </span>
                  </div>
                ))}
              </div>
            </section>
          </>
        ) : (
          <p>Yüklənir…</p>
        )}
      </main>
    </div>
  );
}
createRoot(document.getElementById("root")!).render(<App />);
