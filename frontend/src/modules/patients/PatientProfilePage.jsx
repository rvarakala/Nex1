/**
 * Patient Profile — single patient with 7 sub-tabs:
 *   History · Appointments · Notes · Follow-ups · Payments · Reports · Service
 *
 * History is auto-derived from existing data (appointments, sessions,
 * invoices, payments, service tickets, notes) — no migration required.
 *
 * Inspired by the 7Health.Pro reference: white surface, indigo active tab,
 * timeline dots, "Add Item ▾" CTA, prev/next arrows.
 */
import React, { useEffect, useState, useCallback, useMemo } from 'react';
import { Link, useParams, useNavigate, useSearchParams } from 'react-router-dom';
import axios from 'axios';
import {
  ArrowLeft, ArrowRight, Phone, Calendar, Edit, Plus, Activity,
  CalendarDays, StickyNote, Repeat, Receipt, FileText, Wrench,
  Cake, Heart, Send, GitMerge, HandCoins,
} from 'lucide-react';
import DpdpaActions from './DpdpaActions';
import MergePatientsModal from './MergePatientsModal';
import FamilyChipStrip from './FamilyChipStrip';
import { useAuth } from '../../AuthContext';
import AdvanceReceiptModal from '../billing/AdvanceReceiptModal';
// Naive-UTC-aware datetime helpers — backend stores `datetime.utcnow()`
// without a `Z` marker, so we MUST convert here to render local time.
import { parseUtcIso, fmtDate as fmtDateShared, fmtDateTime as fmtDateTimeShared } from '../../utils/datetime';

const API = process.env.REACT_APP_BACKEND_URL + '/api';

const TABS = [
  { id: 'history',      label: 'History',      icon: Activity },
  { id: 'appointments', label: 'Appointments', icon: CalendarDays },
  { id: 'notes',        label: 'Notes',        icon: StickyNote },
  { id: 'followups',    label: 'Follow-ups',   icon: Repeat },
  { id: 'payments',     label: 'Payments',     icon: Receipt },
  { id: 'advances',     label: 'Advances',     icon: HandCoins },
  { id: 'reports',      label: 'Reports',      icon: FileText },
  { id: 'service',      label: 'Service',      icon: Wrench },
];

const fmtDate = (iso) => fmtDateShared(iso);
const fmtDateTime = (iso) => fmtDateTimeShared(iso);
const initials = (name) => (name || '?').trim().split(/\s+/).slice(0, 2).map(s => s[0] || '').join('').toUpperCase();

// FOLLOW-001 · Sprint-3B — identify a Follow-up appointment.
// The pre-Sprint-3B code filtered by `a.is_followup` which is not a field
// written by any create path (Appointment model has no such attribute).
// The canonical signal is `service === 'Follow-up'` (present in the
// APPOINTMENT_SERVICES catalogue and already used by the DB — see the
// distinct-services snapshot in the NAV-005 audit). We match case- and
// hyphenation-insensitively so historically-typed variants ("Follow up",
// "follow-up", "FOLLOWUP") still surface. `visit_type` never carried a
// "follow-up" value; kept out of the predicate to avoid false positives.
const isFollowupAppointment = (a) => {
  const s = String(a?.service || '').toLowerCase().replace(/[\s_-]/g, '');
  return s.includes('followup');
};

export default function PatientProfilePage() {
  const { patientId } = useParams();
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const { user } = useAuth();
  // Read `?tab=payments` (or any other tab id) so deep-links from the
  // Inventory Board's invoice popup land the receptionist directly on
  // the Payments ledger without an extra click.
  const initialTab = TABS.some(t => t.id === searchParams.get('tab'))
    ? searchParams.get('tab')
    : 'history';
  const [tab, setTab] = useState(initialTab);
  const [showMerge, setShowMerge] = useState(false);
  const canMerge = !!user && ['clinic_owner', 'super_admin', 'founder'].includes(user.role);

  // APPT-005 · Sprint-3B — deep-link highlight for a specific appointment.
  // ModernDashboard (NAV-004 Sprint-2) emits URLs shaped like
  //   /patients/<pid>?tab=appointments&appointment=<appointment_id>
  // The Appointments tab uses this id to scroll+flash the matching row.
  // We capture the value on mount and then STRIP it from the URL so a
  // browser refresh doesn't re-flash on every reload (state, not identity).
  const [highlightAppointmentId, setHighlightAppointmentId] = useState(
    searchParams.get('appointment') || null,
  );
  useEffect(() => {
    if (!searchParams.get('appointment')) return;
    // Preserve `tab` (and any other benign params); drop only `appointment`.
    const next = new URLSearchParams(searchParams);
    next.delete('appointment');
    setSearchParams(next, { replace: true });
    // We deliberately fire once on mount — state was captured above.
  }, []);

  const [patient, setPatient]           = useState(null);
  const [appointments, setAppointments] = useState([]);
  const [sessions, setSessions]         = useState([]);
  const [invoices, setInvoices]         = useState([]);
  const [tickets, setTickets]           = useState([]);
  const [notes, setNotes]               = useState([]);
  const [advances, setAdvances]         = useState([]); // Advance Receipts (Phase 2A)
  const [advanceModalOpen, setAdvanceModalOpen] = useState(false);
  const [greetings, setGreetings]       = useState([]); // pending birthday/anniversary
  const [undoables, setUndoables]       = useState([]); // active merge events in their 10-min window
  const [loading, setLoading]           = useState(true);

  const canCreateAdvance = !!user && ['front_desk', 'accounts', 'clinic_owner', 'super_admin', 'founder'].includes(user.role);
  const canVoidAdvance = !!user && ['accounts', 'clinic_owner', 'super_admin', 'founder'].includes(user.role);

  // Undoable merges are fetched separately (and refreshed on 30s tick)
  // so a stale banner clears itself when the window expires without a
  // full profile reload. Owner-only feature — receptionists can't undo.
  const loadUndoables = useCallback(async () => {
    if (!canMerge) { setUndoables([]); return; }
    try {
      const r = await axios.get(`${API}/patients/${patientId}/undoable-merges`);
      setUndoables(Array.isArray(r.data) ? r.data : []);
    } catch {
      setUndoables([]);
    }
  }, [patientId, canMerge]);
  useEffect(() => {
    loadUndoables();
    // Refresh every 30s so the banner disappears when the window expires
    // even if the user leaves the tab open. Cheap query — indexed by
    // clinic_id + patient_id + undone_at + expires_at.
    const t = setInterval(loadUndoables, 30_000);
    return () => clearInterval(t);
  }, [loadUndoables]);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [p, ap, ses, inv, tk, nt, gr, adv] = await Promise.all([
        axios.get(`${API}/patients/${patientId}`).then(r => r.data).catch(() => null),
        axios.get(`${API}/appointments?patient_id=${patientId}`).then(r => r.data).catch(() => []),
        axios.get(`${API}/sessions?patient_id=${patientId}`).then(r => r.data).catch(() => []),
        axios.get(`${API}/billing/invoices?patient_id=${patientId}`).then(r => r.data).catch(() => []),
        axios.get(`${API}/ha/service-tickets?patient_id=${patientId}`).then(r => r.data?.items || r.data || []).catch(() => []),
        // NOTES-001 · Sprint-3B — call the canonical patient-notes route.
        // Previous URL `/patients/{id}/notes` was never registered, so every
        // patient's Notes tab silently rendered "No notes yet." even when
        // clinical notes existed. `/patient-notes?patient_id=X` is defined
        // in routers/ref_docs.py and enforces tenant scoping via the parent
        // patient lookup.
        axios.get(`${API}/patient-notes?patient_id=${patientId}`).then(r => r.data).catch(() => []),
        axios.get(`${API}/greetings/today?days=30`).then(r => {
          // filter to just this patient (any kind, today or upcoming)
          const all = [...(r.data?.today || []), ...(r.data?.upcoming || [])];
          return all.filter(g => g.patient_id === patientId);
        }).catch(() => []),
        // Advance Receipts (Phase 2A) — scoped to this patient.
        axios.get(`${API}/advance-receipts?patient_id=${patientId}`).then(r => r.data?.items || []).catch(() => []),
      ]);
      setPatient(p);
      setAppointments(Array.isArray(ap) ? ap : []);
      setSessions(Array.isArray(ses) ? ses : []);
      setInvoices(Array.isArray(inv) ? inv : (inv?.items || []));
      setTickets(Array.isArray(tk) ? tk : []);
      setNotes(Array.isArray(nt) ? nt : []);
      setGreetings(gr);
      setAdvances(Array.isArray(adv) ? adv : []);
    } finally { setLoading(false); }
  }, [patientId]);
  useEffect(() => { load(); }, [load]);

  const sendGreeting = useCallback(async (kind) => {
    try {
      const r = await axios.post(`${API}/greetings/${patientId}/send`, { kind });
      if (r.data?.wa_link) window.open(r.data.wa_link, '_blank', 'noopener');
      setGreetings((arr) => arr.map(g => g.kind === kind ? { ...g, already_sent_today: true } : g));
    } catch (e) {
      // eslint-disable-next-line no-alert
      alert('Could not send greeting: ' + (e?.response?.data?.detail || e.message));
    }
  }, [patientId]);

  // Auto-derived timeline — newest first.
  // For imported events, prefer `start_at` (the original visit date) over
  // `created_at` (which is the bulk-import timestamp) so the timeline reflects
  // actual clinical chronology, not data-entry order.
  const timeline = useMemo(() => {
    const events = [];
    if (patient?.created_at) events.push({ at: patient.created_at, kind: 'patient_added', label: 'Patient registered', detail: `MRD ${patient.mrd || patient.patient_id}` });
    appointments.forEach((a) => {
      const isImported = !!a.imported_via;
      events.push({
        at: isImported ? (a.start_at || a.created_at) : (a.created_at || a.start_at),
        kind: 'appointment',
        label: `Visit · ${a.status || 'scheduled'}`,
        detail: [
          (a.recommended_tests || []).join(' + ') || a.service || '',
          a.referred_by ? `Ref: ${a.referred_by}` : '',
          a.notes ? `Dx: ${a.notes}` : '',
        ].filter(Boolean).join(' · '),
        imported: isImported,
      });
    });
    sessions.forEach((s) => events.push({
      at: s.created_at, kind: 'session',
      label: `Diagnostics ${(s.report_status || s.status || 'started').replace(/_/g, ' ')}`,
      detail: s.report_status === 'handed_over' ? 'Report handed over' : (s.tests_done || s.test_methods || []).join?.(', ') || '',
    }));
    invoices.forEach((i) => {
      const isImported = !!i.imported_via;
      events.push({
        at: isImported ? (i.invoice_date || i.created_at) : i.created_at,
        kind: 'invoice',
        label: `Invoice ${i.invoice_no} · ₹${Number(i.rounded_total || i.grand_total || 0).toLocaleString('en-IN')}`,
        detail: `Status: ${i.status}${i.external_invoice_no ? ` · Bill ${i.external_invoice_no}` : ''}`,
        link: `/billing/invoice/${i.invoice_id}`,
        imported: isImported,
      });
    });
    (invoices || []).forEach((i) => (i.payments || []).forEach((p) => events.push({
      at: p.paid_at, kind: 'payment',
      label: `Payment ₹${Number(p.amount || 0).toLocaleString('en-IN')} via ${p.method}`,
      detail: p.reference ? `Ref: ${p.reference}` : '',
      imported: !!i.imported_via,
    })));
    tickets.forEach((t) => events.push({
      at: t.created_at, kind: 'service',
      label: `Service ticket ${t.ticket_no}`,
      detail: `${t.kind} · ${t.status}`,
    }));
    notes.forEach((n) => {
      const isImported = !!n.imported_via;
      events.push({
        at: isImported && n.visit_date ? `${n.visit_date}T00:00:00` : n.created_at,
        kind: 'note',
        label: isImported ? 'Visit log (imported)' : 'Note added',
        detail: n.text,
        imported: isImported,
      });
    });
    return events.filter(e => e.at).sort((a, b) => new Date(b.at) - new Date(a.at));
  }, [patient, appointments, sessions, invoices, tickets, notes]);

  if (loading) return <div className="p-8 text-center italic text-slate-400 text-sm">Loading patient…</div>;
  if (!patient) return (
    <div className="p-8 text-center text-sm text-rose-600">
      Patient not found. <Link to="/patients/list" className="text-indigo-600 underline">Back to list</Link>
    </div>
  );

  // Merged-record banner. When an owner opens `/patients/:id?include_merged=true`
  // (or an old link kept in a bookmark), surface a clear "this record was
  // merged into X" strip so they can jump to the surviving canonical row.
  const mergedBanner = patient.merged_into ? (
    <div className="px-4 sm:px-6 pt-3" data-testid="merged-banner">
      <div className="flex flex-wrap items-center gap-3 border border-slate-300 bg-slate-100 rounded-lg px-3 py-2">
        <GitMerge size={14} className="text-slate-500 flex-shrink-0" />
        <div className="text-[12px] text-slate-700 flex-1 min-w-0">
          This record has been merged into another patient.
          {patient.merged_at && <> Merged on <b>{fmtDate(patient.merged_at)}</b>.</>}
        </div>
        <button
          type="button"
          onClick={() => navigate(`/patients/${patient.merged_into}`)}
          data-testid="merged-open-survivor"
          className="text-[11px] font-bold text-indigo-700 hover:text-indigo-900 underline"
        >Open surviving record →</button>
      </div>
    </div>
  ) : null;

  return (
    <div className="bg-slate-50 min-h-full" data-testid={`patient-profile-${patientId}`}>
      {showMerge && (
        <MergePatientsModal
          secondary={patient}
          onClose={() => setShowMerge(false)}
        />
      )}
      {/* Undo banners — one per active merge event in the 10-min grace
          window. Both the surviving primary and the merged secondary
          see this: owners can reverse from either side. */}
      {undoables.map((ev) => (
        <MergeUndoBanner
          key={ev.merge_id}
          event={ev}
          currentPatientId={patientId}
          onUndone={async () => {
            // On successful undo, refresh EVERYTHING — the secondary is
            // now active again, the primary's history shrinks by the
            // rows that got reverted, and the banner should disappear.
            await Promise.all([load(), loadUndoables()]);
          }}
        />
      ))}
      {/* Top bar */}
      <div className="bg-white border-b border-slate-200 px-4 sm:px-6 py-3 flex items-center justify-between gap-3">
        <button
          onClick={() => navigate('/patients/list')}
          data-testid="profile-back"
          className="inline-flex items-center gap-1 text-[12px] font-semibold text-slate-600 hover:text-slate-900">
          <ArrowLeft size={14} /> Back
        </button>
        <div className="flex items-center gap-1.5">
          <button className="px-2 py-1.5 text-slate-400 hover:text-slate-700" title="Previous patient" disabled><ArrowLeft size={14} /></button>
          <button className="px-2 py-1.5 text-slate-400 hover:text-slate-700" title="Next patient" disabled><ArrowRight size={14} /></button>
        </div>
      </div>

      {/* Patient header */}
      <header className="bg-white border-b border-slate-200 px-4 sm:px-6 py-4 flex flex-wrap items-center gap-4">
        <span className="w-14 h-14 rounded-full bg-gradient-to-br from-indigo-500 to-violet-600 text-white flex items-center justify-center font-black text-lg flex-shrink-0">
          {initials(patient.name)}
        </span>
        <div className="flex-1 min-w-0">
          <h1 className="text-xl font-bold text-slate-900 truncate">{patient.name}</h1>
          <div className="flex flex-wrap items-center gap-x-3 gap-y-1 mt-1 text-[12px] text-slate-600">
            <Pill icon={null}>{patient.gender}</Pill>
            <Pill icon={Calendar}>{patient.age ? `${patient.age} y` : '—'}</Pill>
            {patient.mobile && <Pill icon={Phone}>{patient.mobile}</Pill>}
            <Pill icon={null}>MRD {patient.mrd || patient.patient_id}</Pill>
            {patient.whatsapp_consent && (
              <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full bg-emerald-50 border border-emerald-200 text-emerald-700 text-[10.5px] font-semibold">
                ✓ WhatsApp opt-in
              </span>
            )}
          </div>
        </div>
        <div className="flex items-center gap-2">
          <Link
            to={`/patients/appointments?bookForPatientId=${encodeURIComponent(patient.patient_id)}&bookForPatientName=${encodeURIComponent(patient.name || '')}`}
            data-testid="profile-add-item"
            className="inline-flex items-center gap-1.5 text-[12px] px-3 py-2 bg-indigo-600 hover:bg-indigo-700 text-white rounded-lg font-semibold shadow-sm shadow-indigo-600/20">
            <Plus size={13} /> Add Appointment
          </Link>
          <Link
            to={`/patients/${patient.patient_id}/edit`}
            data-testid="profile-edit"
            className="inline-flex items-center gap-1.5 text-[12px] px-3 py-2 border border-slate-200 hover:border-slate-300 bg-white rounded-lg text-slate-700 font-semibold">
            <Edit size={13} /> Edit
          </Link>
          {canMerge && !patient.merged_into && (
            <button
              type="button"
              onClick={() => setShowMerge(true)}
              data-testid="profile-merge"
              title="Merge this record into another patient"
              className="inline-flex items-center gap-1.5 text-[12px] px-3 py-2 border border-slate-200 hover:border-indigo-300 hover:text-indigo-700 bg-white rounded-lg text-slate-700 font-semibold">
              <GitMerge size={13} /> Merge
            </button>
          )}
        </div>
      </header>

      {mergedBanner}
      {/* Family group chips — visible to all roles. Only renders once
          the patient is loaded so we don't fetch /family for null. */}
      {patient && !patient.merged_into && (
        <FamilyChipStrip patient={patient} />
      )}

      {/* Birthday / Anniversary banner — only when there's a pending occasion */}
      {greetings.length > 0 && (
        <div className="px-4 sm:px-6 pt-4">
          <div className="space-y-2">
            {greetings.map((g) => {
              const isBday = g.kind === 'birthday';
              const Icon = isBday ? Cake : Heart;
              const heading = g.days_until === 0
                ? (isBday
                    ? `🎂 Birthday today${g.age_years ? ` — turning ${g.age_years}` : ''}!`
                    : `💍 Anniversary today${g.years_together ? ` — ${g.years_together} years together` : ''}!`)
                : `${isBday ? '🎂 Birthday' : '💍 Anniversary'} in ${g.days_until} day${g.days_until === 1 ? '' : 's'}`;
              const tone = isBday
                ? 'from-amber-50 to-rose-50 border-amber-200 text-amber-900'
                : 'from-rose-50 to-pink-50 border-rose-200 text-rose-900';
              return (
                <div
                  key={g.kind}
                  data-testid={`profile-greeting-${g.kind}`}
                  className={`flex items-center gap-3 px-3 py-2.5 rounded-lg border bg-gradient-to-r ${tone}`}>
                  <Icon size={16} className="flex-shrink-0" />
                  <div className="flex-1 text-[12.5px] font-semibold">{heading}</div>
                  {g.already_sent_today ? (
                    <span className="text-[11px] font-bold text-emerald-700 px-2 py-0.5 bg-white border border-emerald-200 rounded-full">✓ Greeting sent</span>
                  ) : (
                    <button
                      onClick={() => sendGreeting(g.kind)}
                      disabled={!patient.mobile}
                      data-testid={`profile-send-greeting-${g.kind}`}
                      title={patient.mobile ? 'Open WhatsApp with greeting' : 'No mobile on file'}
                      className="inline-flex items-center gap-1 text-[11.5px] px-3 py-1.5 bg-indigo-600 hover:bg-indigo-700 disabled:bg-slate-300 disabled:cursor-not-allowed text-white font-semibold rounded-md shadow-sm">
                      <Send size={11} /> Send Greeting
                    </button>
                  )}
                </div>
              );
            })}
          </div>
        </div>
      )}

      {/* Sub tabs */}
      <div className="bg-white border-b border-slate-200 px-4 sm:px-6 flex items-center gap-1 overflow-x-auto">
        {TABS.map((t) => {
          const Icon = t.icon;
          return (
            <button
              key={t.id}
              onClick={() => setTab(t.id)}
              data-testid={`profile-tab-${t.id}`}
              className={`flex items-center gap-1.5 text-[12.5px] font-semibold px-3 py-3 -mb-px border-b-2 transition whitespace-nowrap ${
                tab === t.id
                  ? 'text-indigo-700 border-indigo-600'
                  : 'text-slate-500 border-transparent hover:text-slate-800'}`}>
              <Icon size={13} /> {t.label}
            </button>
          );
        })}
      </div>

      {/* Content */}
      <div className="p-4 sm:p-6 space-y-4">
        {tab === 'history' && <HistoryTab events={timeline} />}
        {tab === 'appointments' && (
          <AppointmentsTab rows={appointments} highlightId={highlightAppointmentId} />
        )}
        {tab === 'notes' && <NotesTab rows={notes} />}
        {tab === 'followups' && <FollowupsTab rows={appointments.filter(isFollowupAppointment)} />}
        {tab === 'payments' && <PaymentsTab invoices={invoices} />}
        {tab === 'advances' && (
          <AdvancesTab
            rows={advances}
            canCreate={canCreateAdvance}
            canVoid={canVoidAdvance}
            onCreate={() => setAdvanceModalOpen(true)}
            onChanged={load}
          />
        )}
        {tab === 'reports' && <ReportsTab sessions={sessions} tickets={tickets} />}
        {tab === 'service' && <ServiceTab tickets={tickets} />}

        {/* Owner-only DPDPA actions — collapsed by default */}
        <DpdpaActions patient={patient} />
      </div>

      {advanceModalOpen && patient && (
        <AdvanceReceiptModal
          open={advanceModalOpen}
          patient={patient}
          onClose={() => setAdvanceModalOpen(false)}
          onSuccess={load}
        />
      )}
    </div>
  );
}

const Pill = ({ icon: Icon, children }) => (
  <span className="inline-flex items-center gap-1 text-[11.5px] text-slate-600">
    {Icon ? <Icon size={11} /> : null} {children}
  </span>
);

const KIND_DOTS = {
  patient_added: 'bg-indigo-500',
  appointment:   'bg-blue-500',
  session:       'bg-violet-500',
  invoice:       'bg-amber-500',
  payment:       'bg-emerald-500',
  service:       'bg-orange-500',
  note:          'bg-slate-400',
};

const HistoryTab = ({ events }) => {
  if (!events.length) return <Empty msg="No activity yet." />;
  return (
    <div className="bg-white border border-slate-200 rounded-xl p-4 sm:p-6" data-testid="profile-history-tab">
      <ol className="space-y-4">
        {events.map((e, i) => (
          <li key={i} className="flex gap-3" data-testid={`history-event-${i}`}>
            <div className="flex flex-col items-center pt-1">
              <span className={`w-2.5 h-2.5 rounded-full ${KIND_DOTS[e.kind] || 'bg-slate-400'}`} />
              {i < events.length - 1 && <span className="flex-1 w-px bg-slate-200 mt-1" />}
            </div>
            <div className="flex-1 pb-2">
              <div className="text-[11px] text-slate-500 font-medium flex items-center gap-2">
                <span>{fmtDateTime(e.at)}</span>
                {e.imported && (
                  <span
                    title="Imported from CSV / Excel — original visit date shown"
                    className="inline-flex items-center px-1.5 py-0 rounded text-[9px] font-bold uppercase tracking-wider bg-blue-100 text-blue-700 border border-blue-200"
                  >
                    Imported
                  </span>
                )}
              </div>
              <div className="text-[13px] font-semibold text-slate-900 mt-0.5">{e.label}</div>
              {e.detail && <div className="text-[12px] text-slate-600 mt-0.5 whitespace-pre-wrap">{e.detail}</div>}
              {e.link && <Link to={e.link} className="text-[11px] text-indigo-600 underline mt-1 inline-block">Open →</Link>}
            </div>
          </li>
        ))}
      </ol>
    </div>
  );
};

// APPT-005 · Sprint-3B — Appointments tab with deep-link highlight.
// When PatientProfilePage passes `highlightId`, we auto-scroll the
// matching row into view and flash a temporary amber ring. If the id
// doesn't match any loaded row, we render normally (no error, no
// blank state). The flash class self-clears after 2.5 s so the row
// returns to its resting style — important for print/screenshot.
const AppointmentsTab = ({ rows, highlightId }) => {
  const rowRefs = React.useRef({});
  const [flashId, setFlashId] = React.useState(null);
  React.useEffect(() => {
    if (!highlightId) return;
    if (!rows.some(a => a.appointment_id === highlightId)) return;
    const el = rowRefs.current[highlightId];
    if (el && typeof el.scrollIntoView === 'function') {
      el.scrollIntoView({ behavior: 'smooth', block: 'center' });
    }
    setFlashId(highlightId);
    const t = setTimeout(() => setFlashId(null), 2500);
    return () => clearTimeout(t);
  }, [highlightId, rows]);

  if (!rows.length) return <Empty msg="No appointments yet." />;
  return (
    <div className="bg-white border border-slate-200 rounded-xl overflow-hidden" data-testid="profile-appointments-tab">
      <table className="w-full text-[12.5px]">
        <thead className="bg-slate-50 text-slate-500 uppercase text-[10px] font-bold tracking-wider">
          <tr>
            <th className="text-left px-4 py-2.5">When</th>
            <th className="text-left px-4 py-2.5">Service</th>
            <th className="text-left px-4 py-2.5">Audiologist</th>
            <th className="text-left px-4 py-2.5">Status</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((a) => {
            const isFlash = flashId === a.appointment_id;
            return (
              <tr
                key={a.appointment_id}
                ref={(el) => { if (el) rowRefs.current[a.appointment_id] = el; }}
                data-testid={isFlash ? 'highlighted-appt' : `appt-row-${a.appointment_id}`}
                data-highlighted={isFlash ? 'true' : undefined}
                className={`border-t border-slate-100 transition-colors duration-500 ${
                  isFlash ? 'bg-amber-50 ring-2 ring-inset ring-amber-300' : ''
                }`}
              >
                <td className="px-4 py-2.5">{fmtDateTime(a.start_at)}</td>
                <td className="px-4 py-2.5">{a.service || '—'}</td>
                <td className="px-4 py-2.5">{a.audiologist_name || '—'}</td>
                <td className="px-4 py-2.5"><StatusPill v={a.status} /></td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
};

const NotesTab = ({ rows }) => {
  if (!rows.length) return <Empty msg="No notes yet." />;
  return (
    <ul className="space-y-3" data-testid="profile-notes-tab">
      {rows.map((n) => (
        <li key={n.note_id} className="bg-white border border-slate-200 rounded-lg p-3">
          <div className="text-[11px] text-slate-500">{fmtDateTime(n.created_at)} · {n.audiologist || 'staff'}</div>
          <div className="text-[13px] text-slate-800 mt-1 leading-relaxed whitespace-pre-wrap">{n.text}</div>
        </li>
      ))}
    </ul>
  );
};

const FollowupsTab = ({ rows }) => {
  if (!rows.length) return <Empty msg="No follow-ups scheduled." />;
  return (
    <div className="bg-white border border-slate-200 rounded-xl p-4" data-testid="profile-followups-tab">
      <ul className="space-y-2 text-[12.5px]">
        {rows.map((a) => (
          <li key={a.appointment_id} className="flex items-center justify-between border-b border-slate-100 pb-2 last:border-0">
            <span>{fmtDateTime(a.start_at)} · {a.service || 'Follow-up'}</span>
            <StatusPill v={a.status} />
          </li>
        ))}
      </ul>
    </div>
  );
};
const AdvancesTab = ({ rows = [], canCreate, canVoid, onCreate, onChanged }) => {
  const activeTotal = rows.filter((r) => r.status === 'active')
    .reduce((sum, r) => sum + Number(r.received_amount || 0), 0);

  const openReceipt = (rid) => {
    window.open(`${API}/advance-receipts/${rid}/receipt.pdf`, '_blank', 'noopener,noreferrer');
  };

  const voidReceipt = async (rid, rno) => {
    // eslint-disable-next-line no-alert
    const reason = window.prompt(`Void ${rno}?\nPlease enter a reason (mandatory):`);
    if (!reason || reason.trim().length < 3) return;
    try {
      await axios.post(`${API}/advance-receipts/${rid}/void`, { reason: reason.trim() });
      onChanged?.();
    } catch (e) {
      // eslint-disable-next-line no-alert
      alert('Could not void: ' + (e?.response?.data?.detail || e.message));
    }
  };

  return (
    <div data-testid="profile-advances-tab" className="space-y-3">
      <div className="flex items-center justify-between">
        <div>
          <div className="text-[11px] uppercase tracking-wider text-slate-500 font-semibold">Active advances</div>
          <div className="text-lg font-bold text-emerald-700" data-testid="advances-active-total">
            ₹{activeTotal.toLocaleString('en-IN', { maximumFractionDigits: 2, minimumFractionDigits: 2 })}
          </div>
        </div>
        {canCreate && (
          <button
            onClick={onCreate}
            data-testid="profile-advances-new-btn"
            className="inline-flex items-center gap-2 px-3 py-1.5 bg-sky-600 text-white text-[12px] font-semibold rounded-lg hover:bg-sky-700 transition shadow-sm"
          >
            <Plus size={13} /> Receive Advance
          </button>
        )}
      </div>
      {rows.length === 0 ? (
        <Empty msg="No advance receipts yet for this patient." />
      ) : (
        <div className="bg-white border border-slate-200 rounded-xl overflow-hidden">
          <table className="w-full text-[12.5px]">
            <thead className="bg-slate-50 text-slate-500 uppercase text-[10px] font-bold tracking-wider">
              <tr>
                <th className="text-left px-4 py-2.5">Receipt #</th>
                <th className="text-left px-4 py-2.5">Date</th>
                <th className="text-right px-4 py-2.5">Amount</th>
                <th className="text-left px-4 py-2.5">Method</th>
                <th className="text-left px-4 py-2.5">Purpose</th>
                <th className="text-left px-4 py-2.5">Status</th>
                <th className="px-4 py-2.5"></th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r, i) => (
                <tr key={r.receipt_id} className="border-t border-slate-100" data-testid={`profile-advances-row-${i}`}>
                  <td className="px-4 py-2.5 font-mono text-[11px]">{r.receipt_no}</td>
                  <td className="px-4 py-2.5">{fmtDateTime(r.received_at)}</td>
                  <td className="px-4 py-2.5 text-right tabular-nums font-bold text-slate-900">
                    ₹{Number(r.received_amount || 0).toLocaleString('en-IN', { maximumFractionDigits: 2, minimumFractionDigits: 2 })}
                  </td>
                  <td className="px-4 py-2.5 capitalize text-slate-700">{String(r.method || '').replace('_', ' ')}</td>
                  <td className="px-4 py-2.5 text-slate-600 text-[11.5px]">{r.purpose_note || '—'}</td>
                  <td className="px-4 py-2.5">
                    <span
                      className={`text-[10px] px-1.5 py-0.5 font-bold rounded border uppercase tracking-wider ${
                        r.status === 'voided'
                          ? 'bg-rose-100 text-rose-700 border-rose-300 line-through'
                          : 'bg-emerald-100 text-emerald-800 border-emerald-300'
                      }`}
                      data-testid={`profile-advances-status-${i}`}
                    >
                      {r.status}
                    </span>
                  </td>
                  <td className="px-4 py-2.5 text-right space-x-1 whitespace-nowrap">
                    <button
                      onClick={() => openReceipt(r.receipt_id)}
                      data-testid={`profile-advances-print-btn-${i}`}
                      className="text-[11px] text-sky-700 hover:text-sky-900 font-semibold"
                    >
                      Print →
                    </button>
                    {canVoid && r.status === 'active' && (
                      <button
                        onClick={() => voidReceipt(r.receipt_id, r.receipt_no)}
                        data-testid={`profile-advances-void-btn-${i}`}
                        className="text-[11px] text-rose-700 hover:text-rose-900 font-semibold ml-2"
                      >
                        Void
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
};



const PaymentsTab = ({ invoices }) => {
  if (!invoices.length) return <Empty msg="No invoices yet." />;  return (
    <div className="bg-white border border-slate-200 rounded-xl overflow-hidden" data-testid="profile-payments-tab">
      <table className="w-full text-[12.5px]">
        <thead className="bg-slate-50 text-slate-500 uppercase text-[10px] font-bold tracking-wider">
          <tr>
            <th className="text-left px-4 py-2.5">Invoice</th>
            <th className="text-left px-4 py-2.5">Date</th>
            <th className="text-right px-4 py-2.5">Total</th>
            <th className="text-right px-4 py-2.5">Paid</th>
            <th className="text-right px-4 py-2.5">Due</th>
            <th className="text-left px-4 py-2.5">Status</th>
            <th className="px-4 py-2.5"></th>
          </tr>
        </thead>
        <tbody>
          {invoices.map((i) => (
            <tr key={i.invoice_id} className="border-t border-slate-100">
              <td className="px-4 py-2.5 font-mono text-[11px]">{i.invoice_no}</td>
              <td className="px-4 py-2.5">{fmtDate(i.created_at)}</td>
              <td className="px-4 py-2.5 text-right tabular-nums">₹{Number(i.rounded_total || 0).toLocaleString('en-IN')}</td>
              <td className="px-4 py-2.5 text-right tabular-nums text-emerald-700">₹{Number(i.paid_total || 0).toLocaleString('en-IN')}</td>
              <td className="px-4 py-2.5 text-right tabular-nums text-rose-700">₹{Number(i.due_total || 0).toLocaleString('en-IN')}</td>
              <td className="px-4 py-2.5"><StatusPill v={i.status} /></td>
              <td className="px-4 py-2.5 text-right">
                <Link to={`/billing/invoice/${i.invoice_id}`} className="text-[11px] text-indigo-600 hover:text-indigo-800 font-semibold">Open →</Link>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
};

const ReportsTab = ({ sessions, tickets }) => {
  if (!sessions.length && !tickets.length) return <Empty msg="No reports generated yet." />;
  return (
    <div className="space-y-4" data-testid="profile-reports-tab">
      {sessions.length > 0 && (
        <div className="bg-white border border-slate-200 rounded-xl overflow-hidden">
          <div className="px-4 py-2.5 bg-slate-50 text-[11px] font-bold uppercase tracking-wider text-slate-500">Diagnostic Reports</div>
          <ul className="divide-y divide-slate-100 text-[12.5px]">
            {sessions.map((s) => (
              <li key={s.session_id} className="px-4 py-3 flex items-center justify-between">
                <div>
                  <div className="font-semibold text-slate-900">{(s.test_methods || s.tests_done || []).join?.(', ') || 'Diagnostic session'}</div>
                  <div className="text-[11px] text-slate-500">{fmtDateTime(s.created_at)} · {(s.report_status || 'draft').replace(/_/g, ' ')}</div>
                </div>
                <Link to={`/test/audiogram/${s.session_id}`} className="text-[11px] text-indigo-600 hover:text-indigo-800 font-semibold">Open →</Link>
              </li>
            ))}
          </ul>
        </div>
      )}
      {tickets.length > 0 && (
        <div className="bg-white border border-slate-200 rounded-xl overflow-hidden">
          <div className="px-4 py-2.5 bg-slate-50 text-[11px] font-bold uppercase tracking-wider text-slate-500">Hearing-Aid Service Reports</div>
          <ul className="divide-y divide-slate-100 text-[12.5px]">
            {tickets.map((t) => (
              <li key={t.ticket_no} className="px-4 py-3 flex items-center justify-between">
                <div>
                  <div className="font-semibold text-slate-900">{t.ticket_no} · {t.kind}</div>
                  <div className="text-[11px] text-slate-500">{fmtDateTime(t.created_at)} · {t.status}</div>
                </div>
                <Link to={`/repair`} className="text-[11px] text-indigo-600 hover:text-indigo-800 font-semibold">Open →</Link>
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
};

// SRV-001 · Sprint-3B — Service tab with row-level drill-down.
// Each ticket now has an "Open →" link that navigates to
// /repair/jobs?ticket=<ticket_no> — ServiceTicketsPage opens the
// AudinexaPipelineDrawer for that ticket automatically (see the
// `?ticket=` handling in that page). patient_id is preserved on the
// ticket document itself, and the destination page is tenant-scoped,
// so no other patient's ticket can be accidentally opened.
const ServiceTab = ({ tickets }) => {
  if (!tickets.length) return <Empty msg="No service tickets yet." />;
  return (
    <div className="bg-white border border-slate-200 rounded-xl overflow-hidden" data-testid="profile-service-tab">
      <table className="w-full text-[12.5px]">
        <thead className="bg-slate-50 text-slate-500 uppercase text-[10px] font-bold tracking-wider">
          <tr>
            <th className="text-left px-4 py-2.5">Ticket</th>
            <th className="text-left px-4 py-2.5">Kind</th>
            <th className="text-left px-4 py-2.5">Status</th>
            <th className="text-left px-4 py-2.5">Created</th>
            <th className="px-4 py-2.5"></th>
          </tr>
        </thead>
        <tbody>
          {tickets.map((t) => (
            <tr key={t.ticket_no} className="border-t border-slate-100">
              <td className="px-4 py-2.5 font-mono text-[11px]">{t.ticket_no}</td>
              <td className="px-4 py-2.5">{t.kind}</td>
              <td className="px-4 py-2.5"><StatusPill v={t.status} /></td>
              <td className="px-4 py-2.5">{fmtDate(t.created_at)}</td>
              <td className="px-4 py-2.5 text-right">
                <Link
                  to={`/repair/jobs?ticket=${encodeURIComponent(t.ticket_no)}`}
                  data-testid={`profile-service-open-${t.ticket_no}`}
                  className="text-[11px] text-indigo-600 hover:text-indigo-800 font-semibold"
                >
                  Open →
                </Link>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
};

const Empty = ({ msg }) => (
  <div className="bg-white border border-dashed border-slate-200 rounded-xl py-12 text-center text-sm italic text-slate-400">{msg}</div>
);

const STATUS_TONES = {
  scheduled: 'bg-violet-50 text-violet-700 border-violet-200',
  booked:    'bg-violet-50 text-violet-700 border-violet-200',
  in_queue:  'bg-blue-50 text-blue-700 border-blue-200',
  attending: 'bg-emerald-50 text-emerald-700 border-emerald-200',
  in_progress: 'bg-emerald-50 text-emerald-700 border-emerald-200',
  complete:  'bg-emerald-50 text-emerald-700 border-emerald-200',
  completed: 'bg-emerald-50 text-emerald-700 border-emerald-200',
  paid:      'bg-emerald-50 text-emerald-700 border-emerald-200',
  cancelled: 'bg-rose-50 text-rose-700 border-rose-200',
  draft:     'bg-slate-50 text-slate-700 border-slate-200',
};

const StatusPill = ({ v }) => {
  const k = String(v || '').toLowerCase().replace(/\s/g, '_');
  const tone = STATUS_TONES[k] || 'bg-slate-50 text-slate-700 border-slate-200';
  return (
    <span className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-full border text-[10.5px] font-semibold ${tone}`}>
      {String(v || '—').replace(/_/g, ' ')}
    </span>
  );
};


/* ============================================================================
   MergeUndoBanner — persistent amber strip shown on both the surviving
   primary AND the merged secondary while a merge is inside its 10-min
   undo grace window. One-click reverses everything (row-level rewrites +
   secondary un-soft-mark). Live countdown so the user knows exactly how
   much time is left before the window closes.
   ========================================================================== */
function MergeUndoBanner({ event, currentPatientId, onUndone }) {
  const [now, setNow] = React.useState(() => Date.now());
  const [busy, setBusy] = React.useState(false);
  const [err, setErr] = React.useState('');

  // Tick every second for the countdown. Cheap and only running while
  // the banner is mounted (which is bounded to the 10-min window).
  React.useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, []);

  const expiresMs = new Date(event.expires_at).getTime();
  const secondsLeft = Math.max(0, Math.floor((expiresMs - now) / 1000));
  const mm = Math.floor(secondsLeft / 60).toString().padStart(2, '0');
  const ss = (secondsLeft % 60).toString().padStart(2, '0');

  // The event lives on both sides — the copy differs based on which
  // side of the merge we're currently viewing so the sentence stays
  // grammatical either way.
  const isSecondary = event.role === 'secondary' || currentPatientId === event.secondary_patient_id;
  const otherName = isSecondary ? event.primary_name : event.secondary_name;
  const label = isSecondary
    ? <>This record was <b>merged into {otherName}</b> a moment ago.</>
    : <><b>{otherName}</b> was merged into this record a moment ago.</>;

  const doUndo = async () => {
    setBusy(true); setErr('');
    try {
      await axios.post(`${API}/patients/merge-events/${event.merge_id}/undo`);
      onUndone?.();
    } catch (e) {
      setErr(e?.response?.data?.detail || 'Undo failed');
      setBusy(false);
    }
  };

  if (secondsLeft <= 0) return null;

  return (
    <div className="px-4 sm:px-6 pt-3" data-testid={`merge-undo-banner-${event.merge_id}`}>
      <div className="flex flex-wrap items-center gap-3 border border-amber-300 bg-amber-50 rounded-lg px-3 py-2">
        <span className="text-amber-600 text-base leading-none" aria-hidden>↶</span>
        <div className="text-[12px] text-amber-900 flex-1 min-w-0">
          {label} Moved <b>{event.total_rows_affected}</b> linked row{event.total_rows_affected === 1 ? '' : 's'}.
          <span className="text-amber-700 ml-1">Undo available for <b data-testid={`merge-undo-countdown-${event.merge_id}`}>{mm}:{ss}</b></span>
          {err && <span className="ml-2 text-rose-700">· {err}</span>}
        </div>
        <button
          type="button"
          disabled={busy}
          onClick={doUndo}
          data-testid={`merge-undo-btn-${event.merge_id}`}
          className="inline-flex items-center gap-1 text-[11.5px] font-bold text-white bg-amber-700 hover:bg-amber-800 rounded px-3 py-1.5 disabled:opacity-50"
        >
          {busy ? 'Undoing…' : 'Undo merge'}
        </button>
      </div>
    </div>
  );
}
