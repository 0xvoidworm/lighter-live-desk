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
  pending_orders?: { client_id: number; kind: string; state: string; qty: string; side: string }[];
  protection_id?: number | null; protection_confirmed?: boolean; exit_id?: number | null; journal_ok?: boolean; queue_backlog?: number;
  event_loop_lag_ms?: number; last_order_ack_ms?: number | null; last_cancel_ack_ms?: number | null; reconnects?: Record<string, number>; binance_streams?: { public?: boolean; market?: boolean }; live_blockers?: string[];
  events?: { at: number; kind: string; body: Record<string, unknown> }[];
  coverage?: Coverage;
  feed_channels?: Record<string, { connected: boolean; reconnects: number; last_frame_age_ms: number | null; last_error?: string | null; retry_delay_s: number }>;
  fee_lookup?: { lookup_paused_count: number; unknown_count: number };
  fee_reports?: { covered_unknown_fills: number; reported_fee_total_usdc: string | null; reports: { precision_decimals: number }[] };
  net_cash_audit?: { status: string; net_difference_usdc: string | null; cash_change_usdc?: string } | null;
}

const money = (x?: string | null) => x == null ? '—' : Number(x).toLocaleString('id-ID', { maximumFractionDigits: 4 })
const time = (x?: number) => x ? new Date(x * 1000).toLocaleTimeString('id-ID') : '—'
const channelLabels: Record<string, string> = { binance_public: 'Binance · book/depth', binance_market: 'Binance · transaksi', lighter: 'Lighter · book/trade/ticker' }

function App() {
  const [data, setData] = useState<Status>({ state: 'DISCONNECTED', reason: 'Menghubungkan…' })
  const [notice, setNotice] = useState('')

  useEffect(() => {
    let live = true
    const poll = async () => {
      try {
        const res = await fetch('/api/status', { cache: 'no-store' })
        if (!res.ok) throw new Error(`HTTP ${res.status}`)
        const value = await res.json()
        if (live) setData(value)
      } catch {
        if (live) setData(old => ({ ...old, connected: false, state: 'DISCONNECTED', reason: 'Dashboard terputus dari engine; data terakhir bukan live' }))
      }
    }
    poll()
    const id = setInterval(poll, 1000)
    return () => { live = false; clearInterval(id) }
  }, [])

  async function command(kind: string) {
    setNotice('Mengirim permintaan…')
    try {
      const tokenResponse = await fetch('/api/csrf', { cache: 'no-store' })
      if (!tokenResponse.ok) throw new Error('CSRF tidak tersedia')
      const { token: csrf } = await tokenResponse.json()
      if (!csrf) throw new Error('CSRF tidak tersedia')
      const r = await fetch(`/api/command/${kind}`, { method: 'POST', headers: { 'X-CSRF-Token': csrf } })
      const v = await r.json()
      setNotice(r.ok ? `${v.message} (#${v.command_id})` : `${v.detail || 'Perintah gagal'}`)
    } catch { setNotice('API tidak terhubung; perintah tidak terkirim') }
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
        <div><div className="eyebrow">STATUS OPERASIONAL</div><h1>{data.state || 'DISCONNECTED'}</h1><p>{data.reason || 'Menunggu heartbeat engine'}</p></div>
        <div className="heroMeta"><span className="tag">{data.mode || 'observe'}</span><span>Market ID: {data.market_id ?? '—'}</span><span>{data.entries_stopped ? 'Entry berhenti' : 'Entry diizinkan'}</span></div>
      </section>

      <section className="card readiness"><div className="sectionHead"><h2>Pemanasan & kesiapan data</h2><span className={`pill ${coverage?.ready ? 'good' : 'bad'}`}>{coverage?.ready ? 'Riwayat cukup' : 'Riwayat belum cukup'}</span></div>
        <p className="hint">Koneksi tersambung, quote segar, dan riwayat yang cukup diperiksa terpisah. Probe dan strategi memakai persyaratan coverage yang sama.</p>
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

        <section className="card"><div className="sectionHead"><h2>Kontrol sesi</h2><span className="subtle">{data.session_remaining_s || 0} dtk tersisa · {data.session_positions || 0}/{data.max_session_positions || 3} posisi</span></div>
          <p className="hint">Probe 1× menguji satu transaksi dari status READY; entry strategi tetap berhenti. Perintah dicatat di antrean dan hasil order, cancel, serta close harus dikonfirmasi oleh Lighter.</p>
          <div className="actions"><button disabled={!connected || data.state !== 'READY' || !!data.live_blockers?.length} onClick={() => command('START_LIVE')}>Mulai sesi live</button><button disabled={!connected} onClick={() => command('STOP_ENTRIES')}>Hentikan entry</button><button disabled={!connected} onClick={() => command('CANCEL_ENTRIES')}>Batalkan entry</button><button className="danger" disabled={!connected} onClick={() => command('CLOSE_POSITION')}>Minta tutup posisi</button><button disabled={!connected || data.state !== 'READY' || !data.entries_stopped || !data.probe_blockers || !!data.probe_blockers.length || data.probe_used} onClick={() => command('PROBE_ONCE')}>Probe 1×</button></div>
          {notice && <div className="notice">{notice}</div>}
          <a className="export" href="/api/report" download="lighter-live-desk-report.json">Ekspor laporan JSON ↗</a>
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
