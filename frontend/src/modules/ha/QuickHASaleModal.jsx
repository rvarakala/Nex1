/**
 * QuickHASaleModal — single-form HA sale + fitting + invoice creator.
 *
 * Mounted from FittingLedgerPage. Calls POST /api/ha/quick-sale which
 * atomically writes ha_quick_sales + ha_fittings + invoices docs.
 */
import React, { useEffect, useMemo, useState } from 'react';
import axios from 'axios';
import HASpecPicker from '../../components/HASpecPicker';
import PatientAdvancesBanner from '../billing/PatientAdvancesBanner';

const API = `${process.env.REACT_APP_BACKEND_URL}/api`;

const HA_TYPES = ['BTE', 'RIC', 'ITE', 'ITC', 'CIC', 'IIC', 'POCKET', 'OTHER'];
const HA_TYPE_LABELS = { BTE:'BTE', RIC:'RIC', ITE:'ITE', ITC:'ITC', CIC:'CIC', IIC:'IIC', POCKET:'Pocket Aids', OTHER:'Other' };
const SIDES = [
  { value: 'both',  label: 'Both ears' },
  { value: 'left',  label: 'Left only' },
  { value: 'right', label: 'Right only' },
];
const PAY_MODES = [
  { value: 'cash',          label: 'Cash' },
  { value: 'upi',           label: 'UPI' },
  { value: 'card',          label: 'Card' },
  { value: 'bank_transfer', label: 'Bank transfer' },
  { value: 'cheque',        label: 'Cheque' },
];
const COMMON_BRANDS = ['Phonak', 'ReSound', 'Oticon', 'Signia', 'Widex', 'Starkey', 'Unitron'];

const todayISO = () => new Date().toISOString().slice(0, 10);

function Field({ label, required, hint, children }) {
  return (
    <label className="block">
      <span className="block text-[10px] uppercase tracking-wider text-slate-500 mb-1 font-semibold">
        {label} {required && <span className="text-rose-500">*</span>}
      </span>
      {children}
      {hint && <span className="block text-[10px] text-slate-400 mt-0.5">{hint}</span>}
    </label>
  );
}

export default function QuickHASaleModal({
  onClose, onCreated, prefillPatientId,
  // Additional prefills for the "Convert Trial → Sale" flow —
  // audiologist has already picked brand+model during the trial,
  // so we pre-fill them and leave the fresh saleable-stock serials
  // blank for the audiologist to enter.
  prefillBrand, prefillModel, prefillHaType,
}) {
  const [branches, setBranches] = useState([]);
  const [patients, setPatients] = useState([]);
  const [search, setSearch] = useState('');
  const [picked, setPicked] = useState(null);
  const [saving, setSaving] = useState(false);
  const [err, setErr] = useState('');

  const [form, setForm] = useState({
    branch_id: '',
    brand: prefillBrand || '',
    model: prefillModel || '',
    ha_type: prefillHaType || 'BTE',
    serial_left: '', serial_right: '',
    side: 'both',
    fitting_date: todayISO(),
    warranty_months: 12,
    extended_warranty: false,
    extended_warranty_months: '',
    extended_warranty_source: 'manufacturer',
    mrp: '', sale_price: '',
    discount_amount: '',
    gst_rate: 18,
    payment_status: 'fully_paid',
    payment_mode: 'cash',
    payment_date: todayISO(),
    advance_amount: '',
    expected_payment_date: '',
    notes: '',
  });
  // Device spec — colour + power + wire/tube length. Shape depends on
  // form.side: for 'left'/'right' it's a flat object; for 'both' it's
  // { left: {...}, right: {...} } so audiologist can capture different
  // wire lengths & powers per ear (asymmetric losses are common).
  const [spec, setSpec] = useState({});
  const u = (k) => (e) => setForm((f) => ({ ...f, [k]: e.target.value }));
  const ub = (k) => (e) => setForm((f) => ({ ...f, [k]: e.target.checked }));

  // Live inventory lookup state per side: {status, reason, state}
  // status: 'available' | 'conflict' | 'not_found' | 'checking' | null
  const [serialState, setSerialState] = useState({ left: null, right: null });

  // When the audiologist toggles Side (both ↔ left ↔ right) the spec
  // shape must follow — flat for single-ear, {left, right} for both.
  // Preserve any captured spec by mapping across the transition
  // instead of resetting silently.
  useEffect(() => {
    setSpec((prev) => {
      const isBothShape = prev && (prev.left || prev.right);
      if (form.side === 'both' && !isBothShape) {
        // Flat → both — mirror onto both ears (audiologist can adjust).
        return { left: prev || {}, right: prev || {} };
      }
      if ((form.side === 'left' || form.side === 'right') && isBothShape) {
        // Both → single — keep the matching ear.
        return prev[form.side] || {};
      }
      return prev || {};
    });
  }, [form.side]);

  // Load branches + (optional) prefill patient
  useEffect(() => {
    (async () => {
      try {
        const b = await axios.get(`${API}/branches`);
        setBranches(b.data);
        if (b.data[0]) setForm((f) => ({ ...f, branch_id: b.data[0].branch_id }));
      } catch { /* ignore */ }
      if (prefillPatientId) {
        try {
          const p = await axios.get(`${API}/patients/${prefillPatientId}`);
          setPicked(p.data);
        } catch { /* ignore */ }
      }
    })();
  }, [prefillPatientId]);

  // Patient search (debounced)
  useEffect(() => {
    if (!search || search.length < 2) { setPatients([]); return; }
    const h = setTimeout(async () => {
      try {
        const r = await axios.get(`${API}/patients`, { params: { search, limit: 10 } });
        setPatients(Array.isArray(r.data) ? r.data : []);
      } catch { setPatients([]); }
    }, 200);
    return () => clearTimeout(h);
  }, [search]);

  // Live serial lookup (debounced) — only for sides that are actually visible.
  useEffect(() => {
    const sides = form.side === 'both' ? ['left', 'right'] : [form.side];
    const handles = [];
    sides.forEach((s) => {
      const v = (s === 'left' ? form.serial_left : form.serial_right).trim();
      if (v.length < 3) {
        setSerialState((p) => ({ ...p, [s]: null }));
        return;
      }
      setSerialState((p) => ({ ...p, [s]: { status: 'checking' } }));
      const h = setTimeout(async () => {
        try {
          const r = await axios.get(`${API}/ha/serials/lookup`, { params: { serial_no: v } });
          setSerialState((p) => ({ ...p, [s]: r.data }));
        } catch (e) {
          setSerialState((p) => ({ ...p, [s]: { status: 'error', reason: e?.message } }));
        }
      }, 350);
      handles.push(h);
    });
    return () => handles.forEach(clearTimeout);
  }, [form.serial_left, form.serial_right, form.side]);

  // Auto-calc discount = MRP - sale_price (only if user hasn't manually overridden)
  const discountAuto = useMemo(() => {
    const m = parseFloat(form.mrp || 0);
    const s = parseFloat(form.sale_price || 0);
    if (m > 0 && s >= 0 && m >= s) return Math.max(0, m - s).toFixed(2);
    return '';
  }, [form.mrp, form.sale_price]);

  const balance = useMemo(() => {
    const total = parseFloat(form.sale_price || 0);
    if (form.payment_status === 'fully_paid') return 0;
    if (form.payment_status === 'unpaid') return total;
    return Math.max(0, total - parseFloat(form.advance_amount || 0));
  }, [form.sale_price, form.payment_status, form.advance_amount]);

  const submit = async () => {
    setErr('');
    if (!picked) { setErr('Please pick a patient.'); return; }
    if (!form.branch_id) { setErr('Branch is required.'); return; }
    if (!form.brand.trim()) { setErr('Brand (Make) is required.'); return; }
    if (!form.model.trim()) { setErr('Model is required.'); return; }

    // Side-aware serial validation
    const sLeft = form.serial_left.trim();
    const sRight = form.serial_right.trim();
    if (form.side === 'both') {
      if (!sLeft || !sRight) { setErr('Both ears requires Left AND Right serial numbers.'); return; }
      if (sLeft.toUpperCase() === sRight.toUpperCase()) { setErr('Left and Right serial numbers must be different.'); return; }
    } else if (form.side === 'left' && !sLeft) {
      setErr('Left serial number is required.'); return;
    } else if (form.side === 'right' && !sRight) {
      setErr('Right serial number is required.'); return;
    }
    // Block submit if any visible serial is in 'conflict' state (already SOLD)
    const sidesToCheck = form.side === 'both' ? ['left', 'right'] : [form.side];
    for (const s of sidesToCheck) {
      const st = serialState[s];
      if (st?.status === 'conflict') {
        setErr(`${s.charAt(0).toUpperCase() + s.slice(1)} serial is unavailable: ${st.reason || st.state}`);
        return;
      }
    }
    const mrp = parseFloat(form.mrp);
    const sale = parseFloat(form.sale_price);
    if (!Number.isFinite(mrp) || mrp < 0) { setErr('Enter a valid MRP.'); return; }
    if (!Number.isFinite(sale) || sale < 0) { setErr('Enter a valid sale price.'); return; }
    if (sale > mrp + 0.5) { setErr('Sale price cannot exceed MRP.'); return; }
    if (form.payment_status === 'advance_paid') {
      const adv = parseFloat(form.advance_amount);
      if (!Number.isFinite(adv) || adv <= 0) { setErr('Advance amount required when "Advance paid".'); return; }
      if (adv > sale + 0.5) { setErr('Advance cannot exceed sale price.'); return; }
    }
    setSaving(true);
    try {
      const body = {
        patient_id: picked.patient_id,
        branch_id: form.branch_id,
        brand: form.brand.trim(),
        model: form.model.trim(),
        ha_type: form.ha_type,
        serial_left: form.side === 'right' ? null : (sLeft || null),
        serial_right: form.side === 'left' ? null : (sRight || null),
        side: form.side,
        fitting_date: form.fitting_date,
        warranty_months: parseInt(form.warranty_months) || 12,
        extended_warranty: !!form.extended_warranty,
        extended_warranty_months: form.extended_warranty && form.extended_warranty_months
          ? parseInt(form.extended_warranty_months) : null,
        extended_warranty_source: form.extended_warranty ? form.extended_warranty_source : null,
        mrp, sale_price: sale,
        discount_amount: form.discount_amount !== '' ? parseFloat(form.discount_amount) : null,
        gst_rate: parseFloat(form.gst_rate) || 0,
        payment_status: form.payment_status,
        payment_mode: form.payment_mode || null,
        payment_date: form.payment_date || null,
        advance_amount: form.payment_status === 'advance_paid' ? parseFloat(form.advance_amount) : null,
        expected_payment_date: form.expected_payment_date || null,
        notes: form.notes || null,
        // Device spec (colour + power + length). Backend accepts the
        // whole blob and forwards to inventory / fitting docs so the
        // stock unit knows what wire/tube shipped.
        spec: spec && Object.keys(spec).length ? spec : null,
      };
      const r = await axios.post(`${API}/ha/quick-sale`, body);
      onCreated && onCreated(r.data);
    } catch (e) {
      const d = e?.response?.data?.detail;
      setErr(typeof d === 'string' ? d : (e?.message || 'Save failed.'));
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="fixed inset-0 bg-slate-900/50 z-50 flex items-center justify-center p-4"
         onClick={(e) => { if (e.target === e.currentTarget) onClose(); }}
         data-testid="quick-ha-sale-modal">
      <div className="bg-white rounded-xl shadow-2xl w-full max-w-3xl max-h-[92vh] overflow-auto"
           onClick={(e) => e.stopPropagation()}>
        <div className="sticky top-0 bg-gradient-to-r from-amber-500 to-orange-500 text-white px-5 py-3 flex items-center justify-between">
          <div>
            <h2 className="text-base font-bold">Add Hearing Aid Sale</h2>
            <p className="text-[11px] opacity-90">Records a fitting, sale and invoice in one shot.</p>
          </div>
          <button onClick={onClose} className="text-white/90 hover:text-white text-2xl leading-none" aria-label="Close">×</button>
        </div>

        <div className="p-5 space-y-5">
          {err && <div className="bg-rose-50 border border-rose-200 text-rose-700 text-xs rounded px-3 py-2" data-testid="quick-ha-err">{err}</div>}

          {/* Patient + Branch */}
          <section>
            <h3 className="text-[10px] uppercase tracking-wider text-slate-500 font-bold mb-2">Patient & Branch</h3>
            <div className="grid grid-cols-2 gap-3">
              <Field label="Patient" required>
                {picked ? (
                  <div className="flex items-center gap-2 bg-emerald-50 border border-emerald-200 rounded px-2 py-1.5 text-sm" data-testid="quick-ha-patient">
                    <span className="flex-1 font-semibold">{picked.name}</span>
                    <span className="text-[11px] text-slate-500">{picked.mrd_no || ''}</span>
                    <button onClick={() => setPicked(null)} className="text-rose-500 text-xs hover:underline">✕</button>
                  </div>
                ) : (
                  <>
                    <input value={search} onChange={(e) => setSearch(e.target.value)} placeholder="Search name / mobile / MRD…"
                      data-testid="quick-ha-patient-search"
                      className="w-full border border-slate-300 rounded px-2 py-1.5 text-sm focus:outline-none focus:border-indigo-500" />
                    {patients.length > 0 && (
                      <div className="mt-1 max-h-40 overflow-auto border border-slate-200 rounded">
                        {patients.map((p) => (
                          <button key={p.patient_id} onClick={() => setPicked(p)}
                            data-testid={`quick-ha-patient-pick-${p.patient_id}`}
                            className="block w-full text-left text-xs px-2 py-1 hover:bg-indigo-50">
                            <span className="font-semibold">{p.name}</span>{' '}
                            <span className="text-slate-500">({p.mobile || '—'} · {p.mrd_no || '—'})</span>
                          </button>
                        ))}
                      </div>
                    )}
                  </>
                )}
              </Field>

              <Field label="Branch" required>
                <select value={form.branch_id} onChange={u('branch_id')}
                  data-testid="quick-ha-branch"
                  className="w-full border border-slate-300 rounded px-2 py-1.5 text-sm">
                  {branches.map((b) => <option key={b.branch_id} value={b.branch_id}>{b.name}</option>)}
                </select>
              </Field>
            </div>
            {/* Phase 2B.3 · alert staff if this patient has an existing
                unused Advance Receipt. Informational only — clicking
                opens the Advance Receipts screen where staff apply
                the advance after this sale creates its invoice. */}
            {picked?.patient_id && (
              <div className="mt-3">
                <PatientAdvancesBanner patientId={picked.patient_id} />
              </div>
            )}
          </section>

          {/* Hearing aid */}
          <section>
            <h3 className="text-[10px] uppercase tracking-wider text-slate-500 font-bold mb-2">Hearing Aid</h3>
            <div className="grid grid-cols-3 gap-3">
              <Field label="Make / Brand" required>
                <input list="qha-brands" value={form.brand} onChange={u('brand')} data-testid="quick-ha-brand"
                  placeholder="e.g. Phonak"
                  className="w-full border border-slate-300 rounded px-2 py-1.5 text-sm" />
                <datalist id="qha-brands">
                  {COMMON_BRANDS.map((b) => <option key={b} value={b} />)}
                </datalist>
              </Field>
              <Field label="Model" required>
                <input value={form.model} onChange={u('model')} data-testid="quick-ha-model"
                  placeholder="e.g. Audeo Paradise P50"
                  className="w-full border border-slate-300 rounded px-2 py-1.5 text-sm" />
              </Field>
              <Field label="Type">
                <select value={form.ha_type} onChange={u('ha_type')} data-testid="quick-ha-type"
                  className="w-full border border-slate-300 rounded px-2 py-1.5 text-sm">
                  {HA_TYPES.map((t) => <option key={t} value={t}>{HA_TYPE_LABELS[t] || t}</option>)}
                </select>
              </Field>
              <Field label="Side fitted">
                <select value={form.side} onChange={u('side')} data-testid="quick-ha-side"
                  className="w-full border border-slate-300 rounded px-2 py-1.5 text-sm">
                  {SIDES.map((s) => <option key={s.value} value={s.value}>{s.label}</option>)}
                </select>
              </Field>
              <Field label="Fitting date" required>
                <input type="date" value={form.fitting_date} onChange={u('fitting_date')}
                  data-testid="quick-ha-fitting-date"
                  className="w-full border border-slate-300 rounded px-2 py-1.5 text-sm" />
              </Field>
            </div>

            {/* Side-aware serial fields */}
            <div className={`mt-3 grid gap-3 ${form.side === 'both' ? 'grid-cols-2' : 'grid-cols-1'}`}>
              {(form.side === 'both' || form.side === 'left') && (
                <SerialInputField
                  label={form.side === 'both' ? 'Left ear · Serial number' : 'Left ear · Serial number'}
                  value={form.serial_left}
                  onChange={u('serial_left')}
                  state={serialState.left}
                  testidPrefix="quick-ha-serial-left"
                  placeholder="PHO-RIC-2026LXX"
                />
              )}
              {(form.side === 'both' || form.side === 'right') && (
                <SerialInputField
                  label={form.side === 'both' ? 'Right ear · Serial number' : 'Right ear · Serial number'}
                  value={form.serial_right}
                  onChange={u('serial_right')}
                  state={serialState.right}
                  testidPrefix="quick-ha-serial-right"
                  placeholder="PHO-RIC-2026RXX"
                />
              )}
            </div>

            {/* Device spec — colour + power + wire/tube length. Fields
                shown depend on ha_type (RIC → receiver spec, BTE → power
                class + slim tube; custom shells → colour only). For
                Side = Both ears we render per-ear cards so left and
                right can carry different wires/powers. */}
            <div className="mt-3">
              <HASpecPicker
                deviceType={form.ha_type}
                side={form.side === 'both' ? 'BOTH' : (form.side === 'left' ? 'L' : 'R')}
                value={spec}
                onChange={setSpec}
                testIdPrefix="quick-ha-spec"
                title="Device specification"
              />
            </div>
          </section>

          {/* Warranty */}
          <section>
            <h3 className="text-[10px] uppercase tracking-wider text-slate-500 font-bold mb-2">Warranty</h3>
            <div className="grid grid-cols-3 gap-3 items-end">
              <Field label="Manufacturer warranty (months)">
                <input type="number" min="0" max="240" value={form.warranty_months} onChange={u('warranty_months')}
                  data-testid="quick-ha-warranty-months"
                  className="w-full border border-slate-300 rounded px-2 py-1.5 text-sm" />
              </Field>
              <label className="flex items-center gap-2 text-sm pb-2 col-span-2 cursor-pointer">
                <input type="checkbox" checked={form.extended_warranty} onChange={ub('extended_warranty')}
                  data-testid="quick-ha-extended-warranty"
                  className="rounded text-indigo-600" />
                <span className="font-semibold text-slate-700">Extended warranty offered</span>
              </label>
              {form.extended_warranty && (
                <>
                  <Field label="Extended period (months)">
                    <input type="number" min="0" max="240" value={form.extended_warranty_months} onChange={u('extended_warranty_months')}
                      data-testid="quick-ha-ew-months"
                      className="w-full border border-slate-300 rounded px-2 py-1.5 text-sm" />
                  </Field>
                  <Field label="Source">
                    <select value={form.extended_warranty_source} onChange={u('extended_warranty_source')}
                      data-testid="quick-ha-ew-source"
                      className="w-full border border-slate-300 rounded px-2 py-1.5 text-sm">
                      <option value="manufacturer">Manufacturer / Company</option>
                      <option value="clinic">Clinic-offered</option>
                    </select>
                  </Field>
                </>
              )}
            </div>
          </section>

          {/* Pricing */}
          <section>
            <h3 className="text-[10px] uppercase tracking-wider text-slate-500 font-bold mb-2">Pricing (₹)</h3>
            <div className="grid grid-cols-4 gap-3">
              <Field label="MRP" required>
                <input type="number" min="0" step="0.01" value={form.mrp} onChange={u('mrp')}
                  data-testid="quick-ha-mrp"
                  className="w-full border border-slate-300 rounded px-2 py-1.5 text-sm tabular-nums" />
              </Field>
              <Field label="Sale price" required hint="What the patient pays (incl. GST)">
                <input type="number" min="0" step="0.01" value={form.sale_price} onChange={u('sale_price')}
                  data-testid="quick-ha-sale-price"
                  className="w-full border border-slate-300 rounded px-2 py-1.5 text-sm tabular-nums" />
              </Field>
              <Field label="Discount" hint={discountAuto ? `Auto: ₹${discountAuto}` : undefined}>
                <input type="number" min="0" step="0.01" value={form.discount_amount}
                  onChange={u('discount_amount')}
                  data-testid="quick-ha-discount"
                  placeholder={discountAuto || '0'}
                  className="w-full border border-slate-300 rounded px-2 py-1.5 text-sm tabular-nums" />
              </Field>
              <Field label="GST %">
                <input type="number" min="0" max="28" step="0.5" value={form.gst_rate} onChange={u('gst_rate')}
                  data-testid="quick-ha-gst"
                  className="w-full border border-slate-300 rounded px-2 py-1.5 text-sm tabular-nums" />
              </Field>
            </div>
          </section>

          {/* Payment */}
          <section>
            <h3 className="text-[10px] uppercase tracking-wider text-slate-500 font-bold mb-2">Payment</h3>
            <div className="grid grid-cols-3 gap-3">
              <Field label="Status">
                <select value={form.payment_status} onChange={u('payment_status')}
                  data-testid="quick-ha-pay-status"
                  className="w-full border border-slate-300 rounded px-2 py-1.5 text-sm">
                  <option value="fully_paid">Fully paid</option>
                  <option value="advance_paid">Advance paid</option>
                  <option value="unpaid">Unpaid (bill later)</option>
                </select>
              </Field>
              <Field label="Mode">
                <select value={form.payment_mode} onChange={u('payment_mode')}
                  data-testid="quick-ha-pay-mode"
                  disabled={form.payment_status === 'unpaid'}
                  className="w-full border border-slate-300 rounded px-2 py-1.5 text-sm disabled:bg-slate-100">
                  {PAY_MODES.map((m) => <option key={m.value} value={m.value}>{m.label}</option>)}
                </select>
              </Field>
              <Field label="Payment date">
                <input type="date" value={form.payment_date} onChange={u('payment_date')}
                  data-testid="quick-ha-pay-date"
                  disabled={form.payment_status === 'unpaid'}
                  className="w-full border border-slate-300 rounded px-2 py-1.5 text-sm disabled:bg-slate-100" />
              </Field>
              {form.payment_status === 'advance_paid' && (
                <>
                  <Field label="Advance amount" required>
                    <input type="number" min="0" step="0.01" value={form.advance_amount} onChange={u('advance_amount')}
                      data-testid="quick-ha-advance"
                      className="w-full border border-slate-300 rounded px-2 py-1.5 text-sm tabular-nums" />
                  </Field>
                  <Field label="Balance due (auto)">
                    <input value={`₹ ${balance.toLocaleString('en-IN')}`} readOnly
                      data-testid="quick-ha-balance"
                      className="w-full border border-slate-200 rounded px-2 py-1.5 text-sm bg-slate-50 tabular-nums" />
                  </Field>
                  <Field label="Expected payment date">
                    <input type="date" value={form.expected_payment_date} onChange={u('expected_payment_date')}
                      data-testid="quick-ha-expected"
                      className="w-full border border-slate-300 rounded px-2 py-1.5 text-sm" />
                  </Field>
                </>
              )}
            </div>
          </section>

          {/* Notes */}
          <section>
            <Field label="Notes">
              <textarea rows={2} value={form.notes} onChange={u('notes')}
                data-testid="quick-ha-notes"
                placeholder="e.g. patient prefers right-ear program 2 for restaurants…"
                className="w-full border border-slate-300 rounded px-2 py-1.5 text-sm" />
            </Field>
          </section>

          {/* Footer */}
          <div className="flex justify-end gap-2 pt-3 border-t border-slate-100 sticky bottom-0 bg-white">
            <button onClick={onClose} className="px-4 py-2 text-xs font-semibold text-slate-600 hover:bg-slate-100 rounded">
              Cancel
            </button>
            <button onClick={submit} disabled={saving}
              data-testid="quick-ha-submit"
              className="px-4 py-2 text-xs font-bold bg-amber-600 hover:bg-amber-700 disabled:bg-slate-300 text-white rounded shadow-md">
              {saving ? 'Saving…' : 'Record Sale + Fit + Invoice'}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}


// ─── Serial input with live inventory badge ─────────────────────────

function SerialInputField({ label, value, onChange, state, testidPrefix, placeholder }) {
  // Status pill colour + copy
  let pill = null;
  if (state?.status === 'available') {
    pill = (
      <span className="text-[10px] font-bold text-emerald-700 bg-emerald-50 border border-emerald-200 px-1.5 py-0.5 rounded inline-flex items-center gap-1"
        data-testid={`${testidPrefix}-badge-available`}>
        <span className="w-1.5 h-1.5 rounded-full bg-emerald-500" />In stock
      </span>
    );
  } else if (state?.status === 'conflict') {
    pill = (
      <span className="text-[10px] font-bold text-rose-700 bg-rose-50 border border-rose-200 px-1.5 py-0.5 rounded inline-flex items-center gap-1"
        data-testid={`${testidPrefix}-badge-conflict`}
        title={state.reason || state.state}>
        <span className="w-1.5 h-1.5 rounded-full bg-rose-500" />{state.state || 'Unavailable'}
      </span>
    );
  } else if (state?.status === 'not_found') {
    pill = (
      <span className="text-[10px] font-bold text-amber-700 bg-amber-50 border border-amber-200 px-1.5 py-0.5 rounded inline-flex items-center gap-1"
        data-testid={`${testidPrefix}-badge-untracked`}
        title="Sale will be saved and flagged for inventory reconciliation">
        <span className="w-1.5 h-1.5 rounded-full bg-amber-500" />Not in inventory
      </span>
    );
  } else if (state?.status === 'checking') {
    pill = (
      <span className="text-[10px] font-semibold text-slate-500 bg-slate-100 border border-slate-200 px-1.5 py-0.5 rounded inline-flex items-center gap-1"
        data-testid={`${testidPrefix}-badge-checking`}>
        <span className="w-1.5 h-1.5 rounded-full bg-slate-400 animate-pulse" />Checking…
      </span>
    );
  }

  // Border colour mirrors the status pill
  const borderClass =
    state?.status === 'available' ? 'border-emerald-400 focus:border-emerald-500'
    : state?.status === 'conflict' ? 'border-rose-400 focus:border-rose-500'
    : state?.status === 'not_found' ? 'border-amber-400 focus:border-amber-500'
    : 'border-slate-300 focus:border-indigo-500';

  // Helper text under the input — explains the consequence of the current state
  let hint = null;
  if (state?.status === 'conflict') {
    hint = <span className="text-[10px] text-rose-700 mt-1 block">{state.reason || `Cannot sell — currently ${state.state}.`}</span>;
  } else if (state?.status === 'not_found' && value && value.length >= 3) {
    hint = <span className="text-[10px] text-amber-700 mt-1 block">Will be saved without inventory link — reconcile later via Inventory → Stock Receipt.</span>;
  } else if (state?.status === 'available') {
    hint = <span className="text-[10px] text-emerald-700 mt-1 block">Inventory will decrement by 1 when you submit.</span>;
  }

  return (
    <label className="block">
      <span className="flex items-center justify-between mb-1">
        <span className="block text-[10px] uppercase tracking-wider text-slate-500 font-semibold">
          {label} <span className="text-rose-500">*</span>
        </span>
        {pill}
      </span>
      <input
        value={value}
        onChange={onChange}
        placeholder={placeholder}
        data-testid={`${testidPrefix}-input`}
        className={`w-full border rounded px-2 py-1.5 text-sm font-mono uppercase focus:outline-none focus:ring-1 ${borderClass}`}
      />
      {hint}
    </label>
  );
}

