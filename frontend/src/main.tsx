import React, { useEffect, useState } from 'react'
import { createRoot } from 'react-dom/client'
import './style.css'

type Venue = { valid?: boolean; bid?: string | null; ask?: string | null; age_ms?: number | null; gaps?: number; spread_bps?: string | null; buy_25_vwap?: string | null; sell_25_vwap?: string | null }
type Coverage = { duration_required_s: number; duration_observed_s: number; duration_remaining_s: number; total_samples: number; total_samples_required: number; window_s: number; recent_samples: number; recent_samples_required: number; max_sample_gap_s: number; allowed_gap_s: number; long_gap_count: number; gap_wait_remaining_s: number; ready: boolean; buffer_started_at: number; last_interruption?: { venue: string; channel: string; reason: string; at: number; samples_preserved: number } | null }
type Status = {
  connected?: boolean; state?: string; reason?: string; mode?: string; updated_at?: number;
  market_id?: number | null; quotes?: Record<string, Venue>; fx_usdcusdt?: string | null;
  decision?: { side?: string | null; deviation_bps?: string | null; basis_bps?: string | null; reasons?: string[]; version?: string };
  account?: { equity: string; available: string; position_qty: string; position_price: string; unrealized_pnl: string; realized_pnl: string; funding: string; age_s: number; daily_loss?: string; total_loss?: string; source_funding_budget_remaining?: string | null; effective_exposure_usd?: string; recorded_fees?: string | null; pending_fee_count?: number; account_trading_mode?: number | null; margin_mode?: number | null; initial_margin_fraction?: string | null } | null;
  entries_stopped?: boolean; session_remaining_s?: number; session_positions?: number; max_session_positions?: number;
  probe_used?: boolean; probe_active?: boolean; probe_blockers?: string[];
  probe_accept_reported_fees?: boolean;
  session_accept_reported_fees?: boolean;
  strategy_session?: { id: string; status: string; account_index: number; parameter_hash: string; authorization_expires_epoch: number; max_attempts: number; attempts: { client_id: number }[]; completed_positions: number[]; stop_reason?: string | null } | null;
  allow_live_orders?: boolean; loaded_session_permission_id?: string | null;
  activation_request?: { request_id: string; command_id: number; session_id: string; status: string; reason: string; authorization_expires_epoch: number } | null;
  pending_orders?: { client_id: number; kind: string; state: string; qty: string; side: string }[];
  protection_id?: number | null; protection_confirmed?: boolean; exit_id?: number | null; journal_ok?: boolean; queue_backlog?: number;
  event_loop_lag_ms?: number; last_order_ack_ms?: number | null; last_cancel_ack_ms?: number | null; reconnects?: Record<string, number>; binance_streams?: { public?: boolean; market?: boolean }; live_blockers?: string[];
  events?: { at: number; kind: string; body: Record<string, unknown> }[];
  coverage?: Coverage;
  diagnostic_observation?: { technical_ready: boolean; blockers: string[] };
  history_audit?: { complete: boolean; required: boolean; reason: string; idle_interval_seconds: number; age_seconds: number | null };
  preflight?: { ready_to_send_technically: boolean; send_authorization: boolean; ttl_seconds: number; complete_reads_required: number; send_cost: number; diagnostic: { outcome?: string; elapsed_ms?: number; actual_read_cost?: number } };
  account_state_diagnostic?: { outcome?: string };
  feed_channels?: Record<string, { connected: boolean; reconnects: number; last_frame_age_ms: number | null; last_error?: string | null; retry_delay_s: number }>;
  fee_lookup?: { lookup_paused_count: number; unknown_count: number };
  fee_reports?: { covered_unknown_fills: number; reported_fee_total_usdc: string | null; reports: { precision_decimals: number }[] };
  net_cash_audit?: { status: string; net_difference_usdc: string | null; cash_change_usdc?: string } | null;
}

const money = (x?: string | null) => x == null ? '—' : Number(x).toLocaleString('id-ID', { maximumFractionDigits: 4 })
const time = (x?: number) => x ? new Date(x * 1000).toLocaleTimeString('id-ID') : '—'
const readinessLabels: Record<string, string> = { coverage: 'Menunggu cakupan riwayat harga lengkap', basis_scale: 'Menunggu pembanding harga yang valid', source_freshness: 'Waktu sumber harga belum dapat dipastikan', market_data: 'Data pasar belum memenuhi batas kesegaran', reconciliation_idle: 'Audit akun sedang berjalan' }
const stateLabels: Record<string, string> = { WARMING_UP: 'Menunggu data', READY: 'Siap', IDLE: 'Pasif', LIVE: 'Sesi aktif', PAUSED: 'Entry dihentikan', RECONCILING: 'Memeriksa akun', HALTED: 'Perlu penanganan', DISCONNECTED: 'Terputus' }
const channelLabels: Record<string, string> = { binance_public: 'Binance · book/depth', binance_market: 'Binance · transaksi', lighter: 'Lighter · book/trade/ticker' }

function App() {
  const [data, setData] = useState<Status>({ state: 'DISCONNECTED', reason: 'Menghubungkan…' })
  const [notice, setNotice] = useState('')
  const [sessionPreview, setSessionPreview] = useState<{ proposal: string; can_request: boolean; blockers: string[]; technical_ready: boolean; waiting_blockers?: string[]; request?: { state: string } | null } | null>(null)
  const [startingSession, setStartingSession] = useState(false)
  const [confirmSession, setConfirmSession] = useState(false)
  const waitingActivation = data.activation_request?.status === 'WAITING'
  const waitingLabel = data.activation_request?.reason?.includes('cadangan request') ? 'Menunggu jatah API' : 'Menunggu pemeriksaan sesi'

  useEffect(() => {
    let live = true
    const poll = async () => {
      try {
        const res = await fetch('/api/status', { cache: 'no-store' })
        if (!res.ok) throw new Error(`HTTP ${res.status}`)
        const value = await res.json()
        if (live) setData(value)
        const preview = await fetch('/api/session/preview', { cache: 'no-store' })
        if (preview.ok && live) setSessionPreview(await preview.json())
        else if (live) setSessionPreview(null)
      } catch {
        if (live) setData(old => ({ ...old, connected: false, state: 'DISCONNECTED', reason: 'Dashboard terputus dari engine; data terakhir bukan live' }))
      }
    }
    poll()
    const id = setInterval(poll, 1000)
    return () => { live = false; clearInterval(id) }
  }, [])

  async function startBoundedSession() {
    if (startingSession || !sessionPreview?.can_request || !data.connected) return
    setStartingSession(true)
    setConfirmSession(false)
    const storageKey = `bounded-ui-request:${sessionPreview.proposal}`
    const requestId = sessionStorage.getItem(storageKey) || crypto.randomUUID()
    sessionStorage.setItem(storageKey, requestId)
    try {
      const tokenResponse = await fetch('/api/csrf', { cache: 'no-store' })
      if (!tokenResponse.ok) throw new Error('Kontrol lokal tidak tersedia')
      const { token } = await tokenResponse.json()
      const response = await fetch('/api/session/start', { method: 'POST', headers: { 'X-CSRF-Token': token, 'Content-Type': 'application/json' }, body: JSON.stringify({ request_id: requestId, proposal: sessionPreview.proposal, consent: true }) })
      const result = await response.json()
      setNotice(response.ok ? result.message : result.detail || 'Permintaan ditolak; periksa status sesi')
    } catch {
      setNotice('Respons belum pasti. Permintaan yang sama tetap tersimpan; periksa status sesi, jangan membuat sesi pengganti.')
    } finally { setStartingSession(false) }
  }

  async function command(kind: string) {
    setNotice('Mengirim permintaan…')
    try {
      const tokenResponse = await fetch('/api/csrf', { cache: 'no-store' })
      if (!tokenResponse.ok) throw new Error('CSRF tidak tersedia')
      const { token: csrf } = await tokenResponse.json()
      if (!csrf) throw new Error('CSRF tidak tersedia')
      let binding: Record<string, unknown> | undefined
      if (kind === 'START_WHEN_READY') {
        const session = data.strategy_session
        if (!session || session.status !== 'authorized') throw new Error('Tidak ada otorisasi baru')
        const key = `lighter-activation:${session.id}`
        const requestId = sessionStorage.getItem(key) || crypto.randomUUID()
        sessionStorage.setItem(key, requestId) // Same ID survives uncertain response/reload.
        binding = { request_id: requestId, session_id: session.id, account_index: session.account_index, parameter_hash: session.parameter_hash, authorization_expires_epoch: session.authorization_expires_epoch }
      }
      const r = await fetch(`/api/command/${kind}`, { method: 'POST', headers: { 'X-CSRF-Token': csrf, 'Content-Type': 'application/json' }, body: binding ? JSON.stringify(binding) : undefined })
      const v = await r.json()
      setNotice(r.ok ? `${v.message} (#${v.command_id})` : `${v.detail || 'Perintah gagal'}`)
    } catch { setNotice('Respons belum pasti; periksa request ID dan jurnal sebelum mengulang. Jangan membuat izin pengganti.') }
  }

  const connected = !!data.connected
  const q = connected ? data.quotes || {} : {}
  const account = connected ? data.account : null
  const coverage = connected ? data.coverage : undefined
  const feeReports = connected ? data.fee_reports : undefined
  const cashAudit = connected ? data.net_cash_audit : undefined
  return <div className="page">
    <header className="topbar">
      <div className="brand"><span className="brandmark">L</span><div><strong>Lighter Live Desk</strong><small>BTC perpetual · pengawasan lokal</small></div></div>
      <div className="headerRight"><span className={`dot ${connected ? 'on' : ''}`} />{connected ? 'Engine terhubung' : 'Terputus'}<span className="clock">{time(data.updated_at)}</span></div>
    </header>

    <main>
      <section className="hero">
        <div><div className="eyebrow">STATUS OPERASIONAL</div><h1>{waitingActivation ? waitingLabel : stateLabels[data.state || 'DISCONNECTED'] || 'Memeriksa keadaan'}</h1><p>{data.reason || 'Menunggu heartbeat engine'}</p></div>
        <div className="heroMeta"><span className="tag">{data.mode || 'observe'}</span><span>Market ID: {data.market_id ?? '—'}</span><span>{data.entries_stopped ? 'Entry berhenti' : 'Entry diizinkan'}</span></div>
      </section>

        <section className="card sessionControl"><div className="sectionHead"><h2>Kontrol sesi</h2><span className="subtle">{new Date(Math.max(0, data.session_remaining_s || 0) * 1000).toISOString().slice(11, 19)} tersisa · {data.strategy_session?.attempts.length || 0}/{data.strategy_session?.max_attempts || 3} percobaan entry · {data.strategy_session?.completed_positions.length || 0} posisi selesai</span></div>
          <div className="sessionLimits"><strong>Satu sesi terbatas</strong><p>Maksimal 5 jam (300 menit) · 3 percobaan entry · ukuran minimum legal, nilai maksimal 25 USDC</p><p>Rencana risiko 0,50 USDC per entry · batas loss harian 5 USDC · exposure maksimal 1×equity · cadangan exit 2 USDC.</p><p>Bukti fee resmi hanya berlaku untuk sesi baru ini. Bot menjalankan entry, proteksi dan exit sendiri; tidak ada deposit atau sesi lanjutan otomatis.</p></div>
          <p className="hint">{data.strategy_session?.status === 'active' ? 'Aktif: bot menjalankan strategi dan batas sesi.' : data.strategy_session?.status === 'ending' ? 'Mengakhiri sesi: pantau sampai posisi dan order bersih.' : data.activation_request && ['QUEUED', 'WAITING'].includes(data.activation_request.status) ? 'Menunggu kesiapan: belum aktif, entry berhenti. Persiapan berakhir maksimal 20 menit sejak persetujuan.' : data.strategy_session?.status === 'authorized' ? 'Izin disiapkan, belum aktif. Jika persiapan gagal, hentikan entry dan pantau sampai izin ditutup.' : sessionPreview?.request && sessionPreview.can_request ? 'Sesi sebelumnya selesai — siap membuat sesi baru. Aktivasi memerlukan persetujuan baru dari Anda.' : sessionPreview?.request ? 'Sesi sebelumnya belum dapat diganti. Periksa status dan tunggu pemeriksaan selesai.' : 'Belum aktif. Aktivasi hanya dimulai setelah persetujuan Anda dan pemeriksaan aktual lolos.'}</p>
          <p className="hint">{sessionPreview?.technical_ready ? 'Kesiapan teknis saat ini lolos; bot tetap memeriksa ulang sebelum aktivasi dan setiap entry.' : 'Kesiapan teknis belum lengkap. Permintaan yang sah akan menunggu tanpa mengirim entry.'}</p>
          {!sessionPreview?.technical_ready && !!sessionPreview?.waiting_blockers?.length && <ul className="reasons">{sessionPreview.waiting_blockers.map((reason, i) => <li key={i}>{readinessLabels[reason] || 'Pemeriksaan keselamatan belum lolos'}</li>)}</ul>}
          {data.activation_request && ['QUEUED', 'WAITING'].includes(data.activation_request.status) && <p className="hint">Batas persiapan {time(data.activation_request.authorization_expires_epoch)} · {data.activation_request.status === 'WAITING' ? (data.activation_request.reason || 'Bot menunggu pemeriksaan aktual; sesi belum aktif.') : 'Permintaan diterima; belum aktif.'}</p>}
          {!sessionPreview?.request && sessionPreview?.blockers.length ? <ul className="reasons">{sessionPreview.blockers.map((reason, i) => <li key={i}>{reason}</li>)}</ul> : null}
          <div className="actions"><button disabled={!connected || !sessionPreview?.can_request || startingSession} onClick={() => setConfirmSession(true)}>{sessionPreview?.request && sessionPreview.can_request ? 'Aktifkan sesi baru' : 'Aktifkan satu sesi'}</button><button disabled={!connected} onClick={() => command('STOP_ENTRIES')}>Hentikan entry</button><button className="danger" disabled={!connected} onClick={() => command('CLOSE_POSITION')}>Minta tutup posisi</button></div>
          {confirmSession && <div className="sessionConfirm" role="dialog" aria-label="Konfirmasi satu sesi terbatas"><strong>Aktifkan satu sesi dengan batas di atas?</strong><p>Anda mengizinkan satu sesi baru. Bot menunggu kesiapan paling lama 20 menit, lalu dapat trading otomatis selama maksimal 5 jam (300 menit) dan 3 percobaan entry. Hentikan entry tetap tersedia. Tidak ada jaminan hasil atau fill stop.</p><div className="actions"><button disabled={startingSession || !connected || !sessionPreview?.can_request} onClick={startBoundedSession}>Saya setuju, mulai satu sesi</button><button onClick={() => setConfirmSession(false)}>Batal</button></div></div>}
          {notice && <div className="notice">{notice}</div>}
          <a className="export" href="/api/report" download="lighter-live-desk-report.json">Ekspor laporan JSON ↗</a>
        </section>

      <section className="card readiness"><div className="sectionHead"><h2>Pemanasan & kesiapan data</h2><span className={`pill ${coverage?.ready ? 'good' : 'bad'}`}>{coverage?.ready ? 'Riwayat cukup' : 'Riwayat belum cukup'}</span></div>
        <p className="hint">Koneksi tersambung, quote segar, dan riwayat yang cukup diperiksa terpisah. Probe dan strategi memakai persyaratan coverage yang sama.</p>
        <div className="kv"><span>Siap mengevaluasi sinyal</span><strong>{connected && data.diagnostic_observation?.technical_ready ? 'Ya · kapasitas audit sebelum entry ikut dihitung' : 'Belum'}</strong></div>
        <div className="kv"><span>Preflight lengkap, siap kirim secara teknis</span><strong>{connected && data.preflight?.ready_to_send_technically ? `Ya · bukti maks. ${data.preflight.ttl_seconds} detik` : 'Belum · audit wajib untuk setiap percobaan'}</strong></div>
        <div className="kv"><span>Izin pengiriman</span><strong>{connected && data.preflight?.send_authorization ? 'Aktif sesuai lingkup sesi' : 'OFF'}</strong></div>
        <div className="kv"><span>Audit riwayat lengkap</span><strong>{data.history_audit ? `${data.history_audit.complete ? 'Lengkap' : 'Wajib / belum lengkap'} · umur ${data.history_audit.age_seconds?.toFixed(1) ?? '—'} dtk · berkala ${data.history_audit.idle_interval_seconds} dtk` : '—'}</strong></div>
        <p className="hint">Keadaan akun dan order masing-masing wajib segar maksimal 15 detik. Setiap entry menjalankan audit lengkap lagi ({data.preflight?.complete_reads_required ?? '—'} GET) serta menyisihkan {data.preflight?.send_cost ?? 2} request pengiriman. Bukti diagnostik tidak memberi izin order.</p>
        <p className="hint">{connected && data.diagnostic_observation?.blockers?.length ? `Penghambat teknis: ${data.diagnostic_observation.blockers.join(', ')}` : ''}</p>
        <progress aria-label="Durasi pemanasan" max={coverage?.duration_required_s || 600} value={Math.min(coverage?.duration_observed_s || 0, coverage?.duration_required_s || 600)} />
        <div className="metrics four">
          <div><span>Durasi observasi</span><strong>{coverage ? `${Math.round(coverage.duration_observed_s)} / ${coverage.duration_required_s} dtk` : '—'}</strong></div>
          <div><span>Sampel valid · {coverage?.window_s || 600} dtk terakhir</span><strong>{coverage ? `${coverage.recent_samples} / min. ${coverage.recent_samples_required}` : '—'}</strong></div>
          <div><span>Gap sampel terbesar</span><strong>{coverage ? `${coverage.max_sample_gap_s} / maks. ${coverage.allowed_gap_s} dtk` : '—'}</strong></div>
          <div><span>Gap yang menahan coverage</span><strong>{coverage?.long_gap_count ?? '—'}</strong></div>
        </div>
        {coverage && !coverage.ready && <p className="hint">{coverage.duration_remaining_s > 0 ? `Durasi masih kurang ${Math.ceil(coverage.duration_remaining_s)} detik. ` : ''}{coverage.recent_samples < coverage.recent_samples_required ? `Masih kurang ${coverage.recent_samples_required - coverage.recent_samples} sampel valid. ` : ''}{coverage.gap_wait_remaining_s > 0 ? `Gap lama diperkirakan keluar dari window dalam ${Math.ceil(coverage.gap_wait_remaining_s)} detik jika tidak ada gap baru.` : ''}</p>}
        {connected && Object.entries(data.feed_channels || {}).map(([name, channel]) => <div className="kv" key={name}><span>{channelLabels[name] || name}</span><strong>{channel.connected ? 'Tersambung' : 'Terputus'} · pesan terakhir {channel.last_frame_age_ms == null ? '—' : `${channel.last_frame_age_ms} ms`}{!channel.connected && channel.retry_delay_s > 0 ? ` · jeda ulang ${channel.retry_delay_s} dtk` : ''}</strong></div>)}
        <p className="hint">Buffer dimulai {time(coverage?.buffer_started_at)} karena engine dijalankan ulang. Reconnect mempertahankan sampel; gap riwayat tetap diperiksa.</p>
        {coverage?.last_interruption && <p className="hint">Gangguan terakhir: {coverage.last_interruption.venue}/{coverage.last_interruption.channel}, {coverage.last_interruption.reason}, {time(coverage.last_interruption.at)}. {coverage.last_interruption.samples_preserved} sampel dipertahankan.</p>}
      </section>

      <div className="grid mainGrid">
        <section className="card market"><div className="sectionHead"><h2>Pasar langsung</h2><span className="subtle">Harga bid / ask</span></div>
          <div className="marketRow"><div><span className="venue">BINANCE USDⓈ-M</span><strong>{connected ? money(q.binance?.bid) : '—'} <em>/</em> {connected ? money(q.binance?.ask) : '—'}</strong></div><span className={`pill ${q.binance?.valid && connected ? 'good' : 'bad'}`}>{q.binance?.valid && connected ? `${q.binance.age_ms} ms` : 'Tidak valid'}</span></div>
          <div className="marketRow"><div><span className="venue">LIGHTER BTC PERP</span><strong>{connected ? money(q.lighter?.bid) : '—'} <em>/</em> {connected ? money(q.lighter?.ask) : '—'}</strong></div><span className={`pill ${q.lighter?.valid && connected ? 'good' : 'bad'}`}>{q.lighter?.valid && connected ? `${q.lighter.age_ms} ms` : 'Tidak valid'}</span></div>
          <div className="metrics"><div><span>Deviasi mentah</span><strong>{connected ? money(data.decision?.deviation_bps) : '—'} bps</strong></div><div><span>Basis historis</span><strong>{connected ? money(data.decision?.basis_bps) : '—'} bps</strong></div><div><span>USDC/USDT</span><strong>{connected ? money(data.fx_usdcusdt) : '—'}</strong></div></div>
          <div className="kv"><span>Residual deviasi</span><strong>{connected && data.decision?.deviation_bps && data.decision?.basis_bps ? money(String(Number(data.decision.deviation_bps) - Number(data.decision.basis_bps))) + ' bps' : '—'}</strong></div>
          <div className="kv"><span>Spread Binance / Lighter</span><strong>{connected ? `${money(q.binance?.spread_bps)} / ${money(q.lighter?.spread_bps)} bps` : '—'}</strong></div>
          <div className="kv"><span>VWAP Lighter beli / jual $25</span><strong>{connected ? `${money(q.lighter?.buy_25_vwap)} / ${money(q.lighter?.sell_25_vwap)}` : '—'}</strong></div>
        </section>

        <section className="card account"><div className="sectionHead"><h2>Akun & risiko</h2><span className="subtle">{account ? `Diperbarui ${account.age_s} dtk lalu` : 'Belum terhubung'}</span></div>
          <div className="bigNumber"><span>Equity khusus eksperimen</span><strong>{account ? '$' + money(account.equity) : '—'}</strong></div>
          <div className="kv"><span>Saldo tersedia</span><strong>{account ? '$' + money(account.available) : '—'}</strong></div>
          <div className="kv"><span>Posisi BTC</span><strong>{account ? account.position_qty : '—'}</strong></div>
          <div className="kv"><span>Harga entry fill</span><strong>{account ? '$' + money(account.position_price) : '—'}</strong></div>
          <div className="kv"><span>Unrealized / realized</span><strong>{account ? `${money(account.unrealized_pnl)} / ${money(account.realized_pnl)}` : '—'}</strong></div>
          <div className="kv"><span>Funding</span><strong>{account ? money(account.funding) : '—'}</strong></div>
          <div className="kv"><span>Fee exact dari API</span><strong>{account?.pending_fee_count ? `Belum diketahui (${account.pending_fee_count} fill)` : account ? money(account.recorded_fees) : '—'}</strong></div>
          {feeReports?.covered_unknown_fills ? <div className="kv"><span>Fee dilaporkan ekspor resmi</span><strong>{feeReports.reported_fee_total_usdc} USDC · {feeReports.reports[0]?.precision_decimals} desimal</strong></div> : null}
          {connected && data.probe_accept_reported_fees && <p className="hint">Khusus preflight satu probe, bukti fee ekspor resmi dapat diterima pada presisi laporannya jika audit kas/dana cocok. Fee exact tetap unknown; izin strategi otomatis tetap mati.</p>}
          {connected && data.session_accept_reported_fees && <p className="hint">Bukti fee ekspor resmi berlaku hanya untuk sesi terbatas yang diotorisasi. Sebelum entry berikutnya, posisi/order harus bersih dan audit kas serta bukti setiap fill harus cocok. Fee exact tetap unknown.</p>}
          {data.fee_lookup?.lookup_paused_count ? <p className="hint">Pencarian ulang otomatis dihentikan untuk {data.fee_lookup.lookup_paused_count} fill tanpa fee exact. Fee tidak dianggap nol.</p> : null}
          <div className="kv"><span>Audit net kas</span><strong>{cashAudit?.status === 'matched' ? `Cocok · selisih ${cashAudit.net_difference_usdc} USDC` : cashAudit?.status === 'unexplained_difference' ? `Selisih belum dijelaskan: ${cashAudit.net_difference_usdc} USDC` : 'Perlu audit / bukti terbaru'}</strong></div>
          <div className="kv"><span>Eksposur efektif + entry pending</span><strong>{account ? '$' + money(account.effective_exposure_usd) : '—'}</strong></div>
          <div className="kv"><span>Loss harian / total</span><strong>{account ? `${money(account.daily_loss)} / ${money(account.total_loss)}` : '—'}</strong></div>
          <div className="kv"><span>Sisa budget sumber</span><strong>{account?.source_funding_budget_remaining != null ? '$' + money(account.source_funding_budget_remaining) : 'Belum dicatat'}</strong></div>
          <p className="hint">Budget sumber mencakup biaya transfer dan konversi. Saldo Lighter bukan sisa jatah deposit.</p>
          <div className="kv"><span>Mode akun / margin BTC / fraksi awal</span><strong>{account ? `${account.account_trading_mode ?? '—'} / ${account.margin_mode ?? '—'} / ${account.initial_margin_fraction ?? '—'}` : '—'}</strong></div>
          <div className="kv"><span>Proteksi</span><strong>{data.protection_id ? `#${data.protection_id} · ${data.protection_confirmed ? 'dikonfirmasi bursa' : 'belum dikonfirmasi'}` : 'Belum terkonfirmasi'}</strong></div>
        </section>
      </div>

      <div className="grid bottomGrid">
        <section className="card"><div className="sectionHead"><h2>Keputusan terakhir</h2><span className="tag muted">{data.decision?.version || 'HEURISTIC_UNVALIDATED'}</span></div>
          <div className="decision">{data.decision?.side ? `Sinyal ${data.decision.side.toUpperCase()}` : 'Tidak ada entry'}</div>
          <ul className="reasons">{(data.decision?.reasons || ['Belum ada data']).map((v, i) => <li key={i}>{v.replaceAll('_', ' ')}</li>)}</ul>
          <div className="sectionHead space"><h2>Penghalang probe eksekusi</h2></div>
          <ul className="reasons">{(data.probe_blockers || []).length ? data.probe_blockers?.map((v, i) => <li key={i}>{v}</li>) : <li>{connected ? 'Tidak ada penghalang probe yang terdeteksi' : 'Belum ada data aktual'}</li>}</ul>
          <div className="sectionHead space"><h2>Penghalang strategi otomatis</h2></div>
          <ul className="reasons">{(data.live_blockers || []).length ? data.live_blockers?.map((v, i) => <li key={i}>{v}</li>) : <li>Tidak ada penghalang teknis yang terdeteksi</li>}</ul>
        </section>


      </div>

      <section className="card telemetry"><div className="sectionHead"><h2>Telemetri & jurnal</h2><span className={`pill ${data.journal_ok ? 'good' : 'bad'}`}>Jurnal {data.journal_ok ? 'sehat' : 'belum sehat'}</span></div>
        <div className="metrics four"><div><span>Queue backlog</span><strong>{data.queue_backlog ?? '—'}</strong></div><div><span>Event loop lag</span><strong>{data.event_loop_lag_ms ?? '—'} ms</strong></div><div><span>Reconnect Binance</span><strong>{data.reconnects?.binance ?? '—'}</strong></div><div><span>Reconnect Lighter</span><strong>{data.reconnects?.lighter ?? '—'}</strong></div></div>
        <div className="kv"><span>Binance public / market</span><strong>{connected ? `${data.binance_streams?.public ? 'tersambung' : 'terputus'} / ${data.binance_streams?.market ? 'tersambung' : 'terputus'}` : '—'}</strong></div>
        <div className="metrics four"><div><span>Ack order lokal</span><strong>{data.last_order_ack_ms ?? '—'} ms</strong></div><div><span>Ack cancel lokal</span><strong>{data.last_cancel_ack_ms ?? '—'} ms</strong></div><div><span>Gap Binance</span><strong>{q.binance?.gaps ?? '—'}</strong></div><div><span>Gap Lighter</span><strong>{q.lighter?.gaps ?? '—'}</strong></div></div>
        <div className="sectionHead space"><h2>Order belum final</h2></div><div className="orderList">{data.pending_orders?.length ? data.pending_orders.map(o => <div className="kv" key={o.client_id}><span>#{o.client_id} {o.kind} {o.side} {o.qty}</span><strong>{o.state}</strong></div>) : <p className="subtle">Tidak ada order tercatat yang belum final.</p>}</div>
        <div className="sectionHead space"><h2>Peristiwa terakhir</h2></div><div className="events">{data.events?.length ? data.events.map((e, i) => <div key={i}><time>{time(e.at)}</time><b>{e.kind}</b><span>{JSON.stringify(e.body)}</span></div>) : <p className="subtle">Belum ada peristiwa.</p>}</div>
      </section>
    </main>
  </div>
}

createRoot(document.getElementById('root')!).render(<React.StrictMode><App /></React.StrictMode>)
