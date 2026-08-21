import React, { useState, useEffect, useMemo, useCallback } from 'react';
import axios from 'axios';
import { useNavigate, useLocation } from 'react-router-dom';
import { API, fmtINR, PAYMENT_METHODS } from './billingUtils';
import AddServiceInlineModal from './AddServiceInlineModal';
import ErrorToast, { describeError } from '../../components/ErrorToast';
import LandscapePrompt from '../../components/LandscapePrompt';
import InlineApplyAdvancePanel, { preflightAdvance, allocateAdvance } from './InlineApplyAdvancePanel';

// Compute totals client-side (mirrors backend logic) for live preview.
function resolveDiscount(line, gross) {
  const type = line.discount_type || 'flat';
  const raw = Number(line.discount_value || 0);
  if (type === 'percent') {
    const pct = Math.max(0, Math.min(100, raw));
    return Math.round(gross * pct) / 100 > 0 ? +(gross * pct / 100).toFixed(2) : 0;
  }
  return Math.max(0, Math.min(gross, +Number(raw || 0).toFixed(2)));
}

function computeLinePreview(line, service) {
  const qty = Number(line.quantity || 1);
  const unit = line.unit_price != null ? Number(line.unit_price) : Number(service?.price || 0);
  const isTaxable = line.is_taxable != null ? line.is_taxable : !!service?.is_taxable;
  const gstRate = line.gst_rate != null ? Number(line.gst_rate) : Number(service?.gst_rate || 0);
  const gstInclusive = service?.gst_inclusive !== false;

  const gross = qty * unit;
  const disc = resolveDiscount(line, gross);
  let taxable, tax;
  if (isTaxable && gstRate > 0 && gstInclusive) {
    const netGross = Math.max(0, gross - disc);
    taxable = +(netGross / (1 + gstRate / 100)).toFixed(2);
    tax = +(netGross - taxable).toFixed(2);
  } else if (isTaxable && gstRate > 0) {
    taxable = Math.max(0, gross - disc);
    tax = +(taxable * gstRate / 100).toFixed(2);
  } else {
    taxable = Math.max(0, gross - disc);
    tax = 0;
  }
  return { taxable, tax, total: +(taxable + tax).toFixed(2), gstRate, discountAmount: disc };
}

export default function CreateInvoicePage() {
  const navigate = useNavigate();
  const location = useLocation();
  const preselectPatient = location.state?.patient || null;           // { patient_id, name, mobile, mrd }
  const preselectSession = location.state?.session_id || null;
  // When opened from an HA Sale (e.g. /billing/invoices/new?from_sale=SAL-...),
  // we hydrate patient + lines from a prefill endpoint so HA product details
  // (make, model, serial #, tier) are auto-filled — no manual re-entry.
  const fromSaleNo = new URLSearchParams(location.search).get('from_sale');

  const [services, setServices] = useState([]);
  const [patient, setPatient] = useState(preselectPatient);
  const [patientQuery, setPatientQuery] = useState(preselectPatient?.name || '');
  const [patientResults, setPatientResults] = useState([]);
  const [lines, setLines] = useState([]);
  const [notes, setNotes] = useState('');
  const [patientGstin, setPatientGstin] = useState('');
  const [payNow, setPayNow] = useState({ enabled: false, method: 'cash', amount: '', reference: '' });
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState(null);
  const [showAddSvc, setShowAddSvc] = useState(false);
  const [prefillBanner, setPrefillBanner] = useState(null); // { sale_no, alreadyInvoiced?, invoiceNo? }
  // Phase 2B.3 · Inline Apply-Advance
  const [applyAdv, setApplyAdv] = useState({ enabled: false, receiptId: null, amount: '' });
  const [advWarning, setAdvWarning] = useState('');

  useEffect(() => {
    axios.get(`${API}/billing/services`).then((r) => setServices(r.data || [])).catch(() => {});
  }, []);

  // Hydrate from HA sale prefill (one-shot, on mount when ?from_sale= present).
  useEffect(() => {
    if (!fromSaleNo) return;
    let cancelled = false;
    (async () => {
      try {
        const r = await axios.get(`${API}/ha/sales/${fromSaleNo}/invoice-prefill`);
        if (cancelled) return;
        const d = r.data || {};
        if (d.already_invoiced) {
          setPrefillBanner({ sale_no: fromSaleNo, alreadyInvoiced: true, invoiceNo: d.invoice_no });
          return;
        }
        if (d.patient) {
          setPatient(d.patient);
          setPatientQuery(d.patient.name || '');
        }
        if (Array.isArray(d.lines) && d.lines.length) {
          setLines(d.lines.map((ln) => ({
            key: Math.random().toString(36).slice(2),
            service_id: ln.service_id || null,
            description: ln.description || '',
            quantity: ln.quantity || 1,
            unit_price: ln.unit_price || 0,
            discount_type: ln.discount_type || 'flat',
            discount_value: ln.discount_value || 0,
            is_taxable: !!ln.is_taxable,
            gst_rate: ln.gst_rate || 0,
            product_type: ln.product_type || null,
            make: ln.make || '',
            model: ln.model || '',
            serial_numbers: ln.serial_numbers && ln.serial_numbers.length ? ln.serial_numbers : [''],
            technology_tier: ln.technology_tier || '',
            details_open: true,
          })));
        }
        if (d.notes) setNotes(d.notes);
        setPrefillBanner({ sale_no: fromSaleNo });
      } catch (e) {
        setError(describeError(e) || `Could not load HA sale ${fromSaleNo}`);
      }
    })();
    return () => { cancelled = true; };
  }, [fromSaleNo]);

  // Patient search (debounced)
  useEffect(() => {
    if (patient && patientQuery === patient.name) return;
    if (!patientQuery || patientQuery.trim().length < 2) { setPatientResults([]); return; }
    const t = setTimeout(async () => {
      try {
        const r = await axios.get(`${API}/patients`, { params: { search: patientQuery, limit: 6 } });
        setPatientResults(r.data || []);
      } catch { setPatientResults([]); }
    }, 250);
    return () => clearTimeout(t);
  }, [patientQuery, patient]);

  const svcMap = useMemo(() => Object.fromEntries(services.map((s) => [s.service_id, s])), [services]);

  // Pre-group services by category once per services change (avoids re-computing
  // `.filter(...)` five times on every render of the <select>).
  const SVC_CATEGORIES = ['Consultation', 'Audiology', 'Hearing Aid', 'Accessory'];
  const svcGroups = useMemo(() => {
    const known = SVC_CATEGORIES.map((cat) => ({ cat, items: services.filter((s) => s.category === cat) }))
      .filter(({ items }) => items.length > 0);
    const other = services.filter((s) => !SVC_CATEGORIES.includes(s.category));
    return { known, other };
  }, [services]);

  const addLine = (service_id) => {
    const svc = svcMap[service_id];
    if (!svc) return;
    // Auto-prep product fields when this service is a tracked physical
    // product. Saves the user a click — the panel is open by default and
    // a sensible product_type is preselected.
    const isHa  = svc.category === 'Hearing Aid';
    const isAcc = svc.category === 'Accessory';
    setLines((ls) => [...ls, {
      key: Math.random().toString(36).slice(2),
      service_id,
      description: svc.name,
      quantity: 1,
      unit_price: svc.price,
      discount_type: 'flat',
      discount_value: 0,
      is_taxable: svc.is_taxable,
      gst_rate: svc.gst_rate,
      product_type: isHa ? 'Hearing Aid' : isAcc ? 'Accessory' : null,
      make: '',
      model: '',
      serial_numbers: [''],            // start with one empty slot
      technology_tier: '',
      details_open: isHa || isAcc,     // auto-expand for trackable products
    }]);
  };

  // Inline "Add Service" modal — service is already persisted by the modal,
  // we just merge it into local catalog state and immediately add it as a line.
  const handleServiceCreated = (svc) => {
    setServices((prev) => [...prev, svc]);
    const isHa  = svc.category === 'Hearing Aid';
    const isAcc = svc.category === 'Accessory';
    setLines((ls) => [...ls, {
      key: Math.random().toString(36).slice(2),
      service_id: svc.service_id,
      description: svc.name,
      quantity: 1,
      unit_price: svc.price,
      discount_type: 'flat',
      discount_value: 0,
      is_taxable: svc.is_taxable,
      gst_rate: svc.gst_rate,
      product_type: isHa ? 'Hearing Aid' : isAcc ? 'Accessory' : null,
      make: '',
      model: '',
      serial_numbers: [''],
      technology_tier: '',
      details_open: isHa || isAcc,
    }]);
  };

  const addCustomLine = () => {
    setLines((ls) => [...ls, {
      key: Math.random().toString(36).slice(2),
      service_id: null,
      description: '',
      quantity: 1,
      unit_price: 0,
      discount_type: 'flat',
      discount_value: 0,
      is_taxable: false,
      gst_rate: 0,
      product_type: null,
      make: '',
      model: '',
      serial_numbers: [''],
      technology_tier: '',
      details_open: false,
    }]);
  };

  const updateLine = (key, patch) => {
    setLines((ls) => ls.map((l) => {
      if (l.key !== key) return l;
      const next = { ...l, ...patch };
      // Keep serial-number slots in lockstep with quantity for tracked products.
      if (patch.quantity !== undefined && (next.product_type === 'Hearing Aid' || next.product_type === 'Accessory')) {
        const q = Math.max(1, Math.floor(Number(next.quantity) || 1));
        const cur = next.serial_numbers || [];
        if (cur.length < q) next.serial_numbers = [...cur, ...Array(q - cur.length).fill('')];
        if (cur.length > q) next.serial_numbers = cur.slice(0, q);
      }
      // When user picks/changes the product type, also align slots.
      if (patch.product_type !== undefined && (patch.product_type === 'Hearing Aid' || patch.product_type === 'Accessory')) {
        const q = Math.max(1, Math.floor(Number(next.quantity) || 1));
        if (!next.serial_numbers || next.serial_numbers.length === 0) {
          next.serial_numbers = Array(q).fill('');
        }
      }
      return next;
    }));
  };
  const removeLine = (key) => setLines((ls) => ls.filter((l) => l.key !== key));

  // Totals preview
  const totals = useMemo(() => {
    let subtotal = 0, tax = 0, discount = 0;
    for (const ln of lines) {
      const svc = ln.service_id ? svcMap[ln.service_id] : null;
      const { taxable, tax: t, discountAmount } = computeLinePreview(ln, svc);
      subtotal += taxable;
      tax += t;
      discount += discountAmount;
    }
    const grand = +(subtotal + tax).toFixed(2);
    const rounded = Math.round(grand);
    return {
      subtotal: +subtotal.toFixed(2),
      discount: +discount.toFixed(2),
      tax: +tax.toFixed(2),
      grand,
      rounded,
      round_off: +(rounded - grand).toFixed(2),
    };
  }, [lines, svcMap]);

  const valid = patient && lines.length > 0 && lines.every((l) => (l.description || '').trim().length > 0);

  const submit = async () => {
    if (!valid) return;
    setSaving(true); setError(null); setAdvWarning('');

    // Phase 2B.3 · Inline Apply-Advance validation
    let advToApply = null;
    if (applyAdv.enabled) {
      if (!applyAdv.receiptId) {
        setError('Pick an advance receipt to apply.'); setSaving(false); return;
      }
      const amt = Number(applyAdv.amount);
      if (!Number.isFinite(amt) || amt <= 0) {
        setError('Apply-Advance amount must be > 0.'); setSaving(false); return;
      }
      const cashPortion = payNow.enabled ? Number(payNow.amount || 0) : 0;
      if (amt + cashPortion > Number(totals.rounded || totals.grand) + 0.5) {
        setError(`Advance (₹${amt}) + cash payment (₹${cashPortion}) exceeds invoice total. Reduce one.`);
        setSaving(false); return;
      }
      advToApply = { receiptId: applyAdv.receiptId, amount: amt };
    }

    try {
      // Pre-flight advance re-check
      if (advToApply) {
        try {
          await preflightAdvance(advToApply);
        } catch (pf) {
          setError(pf.message || 'Advance pre-flight failed');
          setSaving(false); return;
        }
      }
      const body = {
        patient_id: patient.patient_id,
        session_id: preselectSession,
        lines: lines.map((l) => {
          // Trim serial_numbers; only include when something was actually entered.
          const serials = (l.serial_numbers || []).map((s) => (s || '').trim()).filter(Boolean);
          return {
            service_id: l.service_id || null,
            description: l.service_id ? null : l.description,
            quantity: Number(l.quantity) || 1,
            unit_price: Number(l.unit_price),
            discount_type: l.discount_type || 'flat',
            discount_value: Number(l.discount_value) || 0,
            is_taxable: l.is_taxable,
            gst_rate: Number(l.gst_rate) || 0,
            product_type: l.product_type || null,
            make: (l.make || '').trim() || null,
            model: (l.model || '').trim() || null,
            serial_numbers: serials.length ? serials : null,
            technology_tier: l.technology_tier || null,
            // Accessory stock plumbing — set when the audiologist uses
            // the Accessory Picker below. Enables the paid-invoice hook
            // to auto-decrement the right (product, branch, variant) row.
            accessory_product_id: l.accessory_product_id || null,
            accessory_variant: l.accessory_variant || null,
          };
        }),
        notes: notes || null,
        patient_gstin: patientGstin || null,
        from_sale_no: fromSaleNo || null,
        initial_payment: payNow.enabled && payNow.amount
          ? { method: payNow.method, amount: Number(payNow.amount), reference: payNow.reference || null }
          : null,
      };
      const r = await axios.post(`${API}/billing/invoices`, body);
      // Phase 2B.3 · Post-allocation via authoritative Phase 2B.2 writer
      if (advToApply && r.data?.invoice_id) {
        try {
          await allocateAdvance({
            receiptId: advToApply.receiptId,
            invoiceId: r.data.invoice_id,
            amount: advToApply.amount,
          });
        } catch (allocErr) {
          const detail = allocErr?.response?.data?.detail || allocErr?.message || 'Advance allocation failed';
          setAdvWarning(
            `Invoice ${r.data.invoice_no || ''} was created, but the advance could not be applied: ${detail}. ` +
            `The invoice remains open at its full balance. Retry via Advance Receipts.`,
          );
          // Still navigate — the invoice EXISTS. The user can retry advance manually.
        }
      }
      navigate(`/billing/invoice/${r.data.invoice_id}`);
    } catch (e) {
      setError(describeError(e, 'Failed to create invoice'));
    } finally { setSaving(false); }
  };

  return (
    <div className="p-4 space-y-3" data-testid="create-invoice-page">
      <LandscapePrompt
        featureKey="billing_create_invoice"
        message="Rotate to landscape for the full invoice editor and live summary."
        testid="billing-create-landscape"
      />
      <div className="grid grid-cols-1 lg:grid-cols-[1fr_320px] gap-3">
      {/* LEFT: form */}
      <div className="space-y-3">
        {prefillBanner && (
          prefillBanner.alreadyInvoiced ? (
            <div className="rounded-lg border border-amber-300 bg-amber-50 px-3 py-2 text-[11px] text-amber-800" data-testid="ci-prefill-already">
              Sale <b className="font-mono">{prefillBanner.sale_no}</b> is already invoiced as <b className="font-mono">{prefillBanner.invoiceNo}</b>. Generate a new invoice only if you really intend to.
            </div>
          ) : (
            <div className="rounded-lg border border-emerald-300 bg-emerald-50 px-3 py-2 text-[11px] text-emerald-800" data-testid="ci-prefill-banner">
              Lines pre-filled from HA sale <b className="font-mono">{prefillBanner.sale_no}</b> — verify make/model/serial and adjust before creating.
            </div>
          )
        )}
        {/* Patient */}
        <div className="bg-white rounded-lg border border-slate-200 p-3 space-y-2">
          <div className="text-[11px] font-bold uppercase tracking-wider text-slate-700">Patient</div>
          <div className="relative">
            <input
              type="text" value={patientQuery}
              onChange={(e) => { setPatientQuery(e.target.value); setPatient(null); }}
              placeholder="Search by name / mobile / MRD…"
              data-testid="ci-patient-search"
              className="w-full px-2 py-1.5 text-xs border border-slate-300 rounded focus:outline-none focus:border-emerald-500"
            />
            {patientResults.length > 0 && !patient && (
              <div className="absolute z-10 mt-0.5 w-full max-h-48 overflow-auto bg-white border border-slate-300 rounded shadow-lg">
                {patientResults.map((p) => (
                  <button key={p.patient_id} type="button"
                    data-testid={`ci-patient-${p.patient_id}`}
                    onClick={() => { setPatient(p); setPatientQuery(p.name); setPatientResults([]); }}
                    className="w-full text-left px-2 py-1 text-xs hover:bg-emerald-50 border-b border-slate-100 last:border-0">
                    <div className="font-semibold">{p.name}</div>
                    <div className="text-[10px] text-slate-500">{p.mrd || p.patient_id}{p.mobile ? ` · ${p.mobile}` : ''}</div>
                  </button>
                ))}
              </div>
            )}
          </div>
          {patient && (
            <div className="grid grid-cols-2 gap-2 text-[11px] bg-slate-50 border border-slate-200 rounded p-2">
              <div><span className="text-slate-500">Name:</span> <b>{patient.name}</b></div>
              <div><span className="text-slate-500">MRD:</span> <b>{patient.mrd || patient.patient_id}</b></div>
              <div><span className="text-slate-500">Mobile:</span> {patient.mobile || '—'}</div>
              <div><span className="text-slate-500">State:</span> {patient.state || '—'}</div>
              <div className="col-span-2">
                <label className="text-[9px] uppercase font-semibold text-slate-500">Patient GSTIN (optional, B2B)</label>
                <input type="text" value={patientGstin} onChange={(e) => setPatientGstin(e.target.value.toUpperCase())}
                  data-testid="ci-patient-gstin"
                  placeholder="15-char GSTIN"
                  maxLength={15}
                  className="w-full px-2 py-1 text-xs border border-slate-300 rounded font-mono" />
              </div>
            </div>
          )}
        </div>

        {/* Phase 2B.3 (UX Correction) · Inline Apply-Advance panel */}
        {patient?.patient_id && (
          <div className="bg-white rounded-lg border border-slate-200 p-3">
            {advWarning && (
              <div className="mb-2 bg-amber-50 border border-amber-300 text-amber-900 text-xs rounded px-3 py-2" data-testid="ci-apply-advance-warning">
                {advWarning}
              </div>
            )}
            <InlineApplyAdvancePanel
              patientId={patient.patient_id}
              salePrice={Number(totals.rounded || totals.grand) || 0}
              value={applyAdv}
              onChange={setApplyAdv}
              disabled={saving}
              testidPrefix="ci-apply-advance"
            />
          </div>
        )}

        {/* Quick add services */}
        <div className="bg-white rounded-lg border border-slate-200 p-3 space-y-2">
          <div className="flex items-center justify-between">
            <div className="text-[11px] font-bold uppercase tracking-wider text-slate-700">Add Service</div>
            <div className="flex items-center gap-1.5">
              <button onClick={() => setShowAddSvc(true)} data-testid="ci-new-service"
                className="text-[10px] px-2 py-0.5 bg-emerald-600 hover:bg-emerald-700 text-white font-semibold rounded shadow-sm">
                + New service
              </button>
              <button onClick={addCustomLine} data-testid="ci-add-custom"
                className="text-[10px] px-2 py-0.5 bg-slate-100 hover:bg-slate-200 text-slate-700 font-semibold rounded">+ Custom line</button>
            </div>
          </div>
          {services.length === 0 ? (
            <div
              data-testid="ci-no-services"
              className="text-xs bg-amber-50 border border-amber-200 rounded px-2.5 py-2 text-amber-800 flex items-center justify-between gap-2"
            >
              <span>
                <b>No services in your catalogue yet.</b> Click <b>+ New service</b> above to add your first one — it will be saved permanently and reused on every future invoice.
              </span>
            </div>
          ) : (
            <select
              onChange={(e) => {
                if (e.target.value === '__new__') { setShowAddSvc(true); e.target.value = ''; return; }
                if (e.target.value) { addLine(e.target.value); e.target.value = ''; }
              }}
              data-testid="ci-add-service"
              defaultValue=""
              className="w-full text-xs border border-slate-300 rounded px-2 py-1.5 bg-white"
            >
              <option value="">— Pick a service from catalogue —</option>
              <option value="__new__" className="font-semibold text-emerald-700">+ Add new service to catalogue…</option>
              {svcGroups.known.map(({ cat, items }) => (
                <optgroup key={cat} label={cat}>
                  {items.map((s) => {
                    const label = `${s.name} — ₹${s.price}${s.is_taxable ? ` (+${s.gst_rate}% GST)` : ' (exempt)'}`;
                    return <option key={s.service_id} value={s.service_id}>{label}</option>;
                  })}
                </optgroup>
              ))}
              {svcGroups.other.length > 0 && (
                <optgroup label="Other">
                  {svcGroups.other.map((s) => {
                    const label = `${s.name} — ₹${s.price}${s.is_taxable ? ` (+${s.gst_rate}% GST)` : ' (exempt)'}`;
                    return <option key={s.service_id} value={s.service_id}>{label}</option>;
                  })}
                </optgroup>
              )}
            </select>
          )}
        </div>

        <AddServiceInlineModal
          open={showAddSvc}
          onClose={() => setShowAddSvc(false)}
          onCreated={handleServiceCreated}
        />

        {/* Lines */}
        <div className="bg-white rounded-lg border border-slate-200 overflow-hidden">
          <table className="w-full text-xs">
            <thead className="bg-slate-50 border-b border-slate-200 text-[10px] uppercase text-slate-500">
              <tr className="text-left">
                <th className="px-2 py-1.5 font-semibold">Description</th>
                <th className="px-2 py-1.5 font-semibold">HSN</th>
                <th className="px-2 py-1.5 font-semibold w-12">Qty</th>
                <th className="px-2 py-1.5 font-semibold w-24 text-right">Unit</th>
                <th className="px-2 py-1.5 font-semibold w-36 text-right">Discount</th>
                <th className="px-2 py-1.5 font-semibold w-16 text-right">GST%</th>
                <th className="px-2 py-1.5 font-semibold w-28 text-right">Total</th>
                <th className="px-2 py-1.5 w-6"></th>
              </tr>
            </thead>
            <tbody data-testid="ci-lines">
              {lines.length === 0 && (
                <tr><td colSpan={8} className="px-3 py-8 text-center text-slate-400 italic">No lines yet. Pick a service from the catalogue above.</td></tr>
              )}
              {lines.map((l) => {
                const svc = l.service_id ? svcMap[l.service_id] : null;
                const p = computeLinePreview(l, svc);
                const hasProductDetails = Boolean(
                  l.product_type || l.make || l.model || l.technology_tier ||
                  (l.serial_numbers || []).some((s) => (s || '').trim()),
                );
                return (
                  <React.Fragment key={l.key}>
                  <tr data-testid={`ci-line-${l.key}`} className="border-b border-slate-100 last:border-0">
                    <td className="px-2 py-1">
                      <input type="text" value={l.description}
                        disabled={!!l.service_id}
                        onChange={(e) => updateLine(l.key, { description: e.target.value })}
                        className="w-full px-1.5 py-1 text-xs border border-slate-200 rounded disabled:bg-slate-50" />
                      <button
                        type="button"
                        onClick={() => updateLine(l.key, { details_open: !l.details_open })}
                        data-testid={`ci-toggle-product-${l.key}`}
                        className={`mt-0.5 text-[10px] font-semibold inline-flex items-center gap-0.5 transition-colors ${
                          l.details_open ? 'text-indigo-700 hover:text-indigo-900' : 'text-slate-500 hover:text-indigo-700'
                        }`}
                      >
                        <span>{l.details_open ? '▾' : '▸'}</span>
                        {hasProductDetails ? 'Product details' : '+ Add product details'}
                        {hasProductDetails && !l.details_open && (
                          <span className="ml-1 px-1 py-px text-[9px] font-bold rounded bg-indigo-100 text-indigo-700">filled</span>
                        )}
                      </button>
                    </td>
                    <td className="px-2 py-1">
                      <span className="text-[10px] font-mono text-slate-500">{svc?.hsn_sac || '—'}</span>
                    </td>
                    <td className="px-2 py-1">
                      <input type="number" value={l.quantity} min="0.01" step="0.5"
                        onChange={(e) => updateLine(l.key, { quantity: e.target.value })}
                        className="w-full px-1 py-1 text-xs border border-slate-200 rounded text-right tabular-nums" />
                    </td>
                    <td className="px-2 py-1">
                      <input type="number" value={l.unit_price} step="1"
                        onChange={(e) => updateLine(l.key, { unit_price: e.target.value })}
                        className="w-full px-1 py-1 text-xs border border-slate-200 rounded text-right tabular-nums" />
                    </td>
                    <td className="px-2 py-1">
                      <div className="flex items-center gap-1">
                        <input type="number" value={l.discount_value} step="1" min="0"
                          max={l.discount_type === 'percent' ? 100 : undefined}
                          onChange={(e) => updateLine(l.key, { discount_value: e.target.value })}
                          data-testid={`ci-discount-value-${l.key}`}
                          className="w-full px-1 py-1 text-xs border border-slate-200 rounded text-right tabular-nums" />
                        <button
                          type="button"
                          onClick={() => updateLine(l.key, { discount_type: l.discount_type === 'percent' ? 'flat' : 'percent' })}
                          data-testid={`ci-discount-toggle-${l.key}`}
                          title={l.discount_type === 'percent' ? 'Switch to ₹ flat' : 'Switch to %'}
                          className={`text-[10px] font-bold px-1.5 py-1 rounded border leading-none transition-colors ${
                            l.discount_type === 'percent'
                              ? 'bg-emerald-600 text-white border-emerald-600 hover:bg-emerald-700'
                              : 'bg-slate-100 text-slate-700 border-slate-300 hover:bg-slate-200'
                          }`}>
                          {l.discount_type === 'percent' ? '%' : '₹'}
                        </button>
                      </div>
                      {l.discount_type === 'percent' && Number(l.discount_value) > 0 && (
                        <div className="text-[9px] text-right text-slate-500 tabular-nums mt-0.5">
                          = {fmtINR(p.discountAmount)}
                        </div>
                      )}
                    </td>
                    <td className="px-2 py-1 text-right text-slate-500">{l.is_taxable ? `${l.gst_rate}%` : 'Exempt'}</td>
                    <td className="px-2 py-1 text-right font-semibold tabular-nums">{fmtINR(p.total)}</td>
                    <td className="px-2 py-1">
                      <button onClick={() => removeLine(l.key)} data-testid={`ci-remove-${l.key}`}
                        className="text-rose-500 hover:text-rose-700 text-sm leading-none">×</button>
                    </td>
                  </tr>
                  {l.details_open && (
                    <tr className="bg-slate-50/70 border-b border-slate-100">
                      <td colSpan={8} className="px-3 py-2.5">
                        <ProductDetailsPanel line={l} onChange={(patch) => updateLine(l.key, patch)} />
                      </td>
                    </tr>
                  )}
                  </React.Fragment>
                );
              })}
            </tbody>
          </table>
        </div>

        {/* Notes */}
        <div className="bg-white rounded-lg border border-slate-200 p-3">
          <label className="block text-[10px] uppercase tracking-wider font-bold text-slate-700 mb-1">Notes / Remarks</label>
          <textarea value={notes} onChange={(e) => setNotes(e.target.value)} rows={2}
            placeholder="e.g., Payment plan, referral notes…"
            data-testid="ci-notes"
            className="w-full px-2 py-1 text-xs border border-slate-300 rounded resize-y" />
        </div>
      </div>

      {/* RIGHT: totals + pay now + submit */}
      <div className="space-y-3">
        <div className="bg-white rounded-lg border-2 border-emerald-300 p-3 space-y-1 sticky top-2">
          <div className="text-[11px] font-bold uppercase tracking-wider text-emerald-700 mb-1">Summary</div>
          <Row label="Subtotal (taxable value)" value={fmtINR(totals.subtotal)} />
          {totals.discount > 0 && <Row label="Discount" value={`−${fmtINR(totals.discount)}`} />}
          {totals.tax > 0 && <Row label="GST total" value={fmtINR(totals.tax)} />}
          <Row label="Grand total" value={fmtINR(totals.grand)} strong />
          {totals.round_off !== 0 && <Row label="Round off" value={fmtINR(totals.round_off)} />}
          <div className="border-t border-emerald-200 pt-1.5 mt-1.5">
            <Row label="Payable" value={fmtINR(totals.rounded)} big />
          </div>
        </div>

        <div className="bg-white rounded-lg border border-slate-200 p-3 space-y-2">
          <div className="flex items-center justify-between">
            <div className="text-[11px] font-bold uppercase tracking-wider text-slate-700">Collect Payment Now?</div>
            <label className="inline-flex items-center gap-1 cursor-pointer">
              <input type="checkbox" checked={payNow.enabled} data-testid="ci-paynow-toggle"
                onChange={(e) => setPayNow({ ...payNow, enabled: e.target.checked, amount: e.target.checked ? totals.rounded : '' })} />
              <span className="text-[10px] text-slate-600">Yes</span>
            </label>
          </div>
          {payNow.enabled && (
            <div className="space-y-1.5">
              <select value={payNow.method} onChange={(e) => setPayNow({ ...payNow, method: e.target.value })}
                data-testid="ci-paynow-method"
                className="w-full text-xs border border-slate-300 rounded px-2 py-1 bg-white">
                {PAYMENT_METHODS.map((m) => <option key={m.value} value={m.value}>{m.label}</option>)}
              </select>
              <input type="number" value={payNow.amount} placeholder="Amount"
                onChange={(e) => setPayNow({ ...payNow, amount: e.target.value })}
                data-testid="ci-paynow-amount"
                className="w-full text-xs border border-slate-300 rounded px-2 py-1 tabular-nums" />
              <input type="text" value={payNow.reference}
                onChange={(e) => setPayNow({ ...payNow, reference: e.target.value })}
                placeholder="Reference (UPI UTR / card last-4 / txn id)"
                data-testid="ci-paynow-ref"
                className="w-full text-xs border border-slate-300 rounded px-2 py-1 font-mono" />
            </div>
          )}
        </div>

        {error && <ErrorToast err={error} testid="ci-error" />}

        <button onClick={submit} disabled={!valid || saving} data-testid="ci-submit"
          className="w-full py-2 text-sm bg-emerald-600 hover:bg-emerald-700 disabled:bg-slate-300 text-white font-bold rounded shadow-sm">
          {saving ? 'Creating invoice…' : 'Create Invoice'}
        </button>
      </div>
      </div>
    </div>
  );
}

const Row = ({ label, value, strong, big }) => (
  <div className={`flex justify-between items-baseline ${big ? 'text-sm' : 'text-xs'}`}>
    <span className={`${big ? 'font-bold text-slate-700' : 'text-slate-600'}`}>{label}</span>
    <span className={`tabular-nums ${big ? 'text-xl font-bold text-emerald-700' : strong ? 'font-bold text-slate-800' : 'text-slate-800'}`}>
      {value}
    </span>
  </div>
);

// ============================================================================
// ProductDetailsPanel — collapsible per-line editor for hearing aid /
// accessory metadata that should print on the invoice. Fields are all
// optional at the API level; the UI nudges sensible defaults based on
// product_type but never blocks save.
// ============================================================================
const TIER_OPTIONS    = ['Basic', 'Essential', 'Standard', 'Advanced', 'Premium'];
const PRODUCT_TYPES   = ['Hearing Aid', 'Accessory', 'Other'];
const POPULAR_MAKES   = ['Phonak', 'Signia', 'ReSound', 'Widex', 'Oticon', 'Starkey', 'Unitron'];

function ProductDetailsPanel({ line, onChange }) {
  const isHa = line.product_type === 'Hearing Aid';
  const isAcc = line.product_type === 'Accessory';
  const qty  = Math.max(1, Math.floor(Number(line.quantity) || 1));
  // Ensure exactly `qty` slots when the panel is open so the UI matches sale qty.
  const serials = (() => {
    const cur = line.serial_numbers || [];
    if (cur.length === qty) return cur;
    if (cur.length < qty) return [...cur, ...Array(qty - cur.length).fill('')];
    return cur.slice(0, qty);
  })();

  const updSerial = (i, v) => {
    const next = [...serials];
    next[i] = v;
    onChange({ serial_numbers: next });
  };

  return (
    <div data-testid={`ci-product-panel-${line.key}`} className="space-y-2">
      <div className="text-[10px] font-bold uppercase tracking-wider text-indigo-700">Product details (optional)</div>

      {/* Accessory Picker — appears above the free-text fields when the
          line is tagged as an Accessory. Picking auto-fills brand, model,
          unit price, GST + attaches the accessory_product_id/variant so
          the paid-invoice hook can auto-decrement stock. Users can still
          hand-edit any field afterwards (e.g. one-off custom pricing). */}
      {isAcc && (
        <AccessoryPicker line={line} onChange={onChange} />
      )}

      <div className="grid grid-cols-2 lg:grid-cols-4 gap-2">
        <FieldLabel label="Product Type">
          <select
            value={line.product_type || ''}
            onChange={(e) => onChange({ product_type: e.target.value || null })}
            data-testid={`ci-product-type-${line.key}`}
            className="w-full px-2 py-1 text-xs border border-slate-300 rounded bg-white"
          >
            <option value="">— Select —</option>
            {PRODUCT_TYPES.map((p) => <option key={p} value={p}>{p}</option>)}
          </select>
        </FieldLabel>

        <FieldLabel label="Make / Brand">
          <input
            list={`makes-${line.key}`}
            value={line.make || ''}
            onChange={(e) => onChange({ make: e.target.value })}
            data-testid={`ci-product-make-${line.key}`}
            placeholder="Phonak, Signia…"
            className="w-full px-2 py-1 text-xs border border-slate-300 rounded"
          />
          <datalist id={`makes-${line.key}`}>
            {POPULAR_MAKES.map((m) => <option key={m} value={m} />)}
          </datalist>
        </FieldLabel>

        <FieldLabel label="Model">
          <input
            value={line.model || ''}
            onChange={(e) => onChange({ model: e.target.value })}
            data-testid={`ci-product-model-${line.key}`}
            placeholder="e.g., Audeo P50-R"
            className="w-full px-2 py-1 text-xs border border-slate-300 rounded"
          />
        </FieldLabel>

        <FieldLabel label="Technology Tier" disabled={!isHa} hint={!isHa ? 'For hearing aids only' : undefined}>
          <select
            value={line.technology_tier || ''}
            onChange={(e) => onChange({ technology_tier: e.target.value || null })}
            disabled={!isHa}
            data-testid={`ci-product-tier-${line.key}`}
            className="w-full px-2 py-1 text-xs border border-slate-300 rounded bg-white disabled:bg-slate-100 disabled:text-slate-400"
          >
            <option value="">— Select —</option>
            {TIER_OPTIONS.map((t) => <option key={t} value={t}>{t}</option>)}
          </select>
        </FieldLabel>
      </div>

      {/* Serial numbers — one input per unit when product_type is HA/Accessory */}
      {(line.product_type === 'Hearing Aid' || line.product_type === 'Accessory') && (
        <div>
          <div className="text-[10px] uppercase tracking-wider font-bold text-slate-500 mb-1">
            Serial Numbers <span className="text-slate-400 font-normal">({qty} unit{qty === 1 ? '' : 's'})</span>
          </div>
          <div className="grid grid-cols-2 lg:grid-cols-3 gap-2">
            {serials.map((s, i) => (
              <input
                key={i}
                value={s || ''}
                onChange={(e) => updSerial(i, e.target.value)}
                data-testid={`ci-product-serial-${line.key}-${i}`}
                placeholder={`Unit ${i + 1} serial #`}
                className="w-full px-2 py-1 text-xs font-mono border border-slate-300 rounded"
              />
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

function FieldLabel({ label, children, disabled, hint }) {
  return (
    <label className="block">
      <span className={`block text-[10px] uppercase tracking-wider font-bold mb-0.5 ${disabled ? 'text-slate-400' : 'text-slate-600'}`}>
        {label} {hint && <span className="ml-1 text-[9px] font-normal text-slate-400 normal-case tracking-normal">· {hint}</span>}
      </span>
      {children}
    </label>
  );
}


/* ============================================================================
   AccessoryPicker — surfaces on a line whose product_type is "Accessory".

   Lets the audiologist choose an accessory SKU (batteries, tips, RIC
   receivers, etc.) from the clinic catalogue instead of hand-typing brand
   and model. When a SKU with variants (e.g., a RIC Receiver with 1M/2M/3M/…)
   is picked, a Variant dropdown appears so the user can pick the exact size.

   Picking auto-fills the free-text fields below (make, model, MRP, GST) so
   the layout below renders coherent data even after the picker is used.
   Also attaches accessory_product_id + accessory_variant to the line so the
   paid-invoice hook can decrement stock deterministically.
   ========================================================================== */
function AccessoryPicker({ line, onChange }) {
  const [products, setProducts] = React.useState([]);
  const [stock, setStock] = React.useState([]);       // stock rows for the picked SKU
  const [loading, setLoading] = React.useState(true);

  // Load accessory catalogue once per panel mount. Cheap: <50 SKUs typical.
  React.useEffect(() => {
    let cancelled = false;
    setLoading(true);
    axios.get(`${API}/ha/products`, {
      params: { form_factor: 'accessory', active: true },
    }).then((r) => {
      if (!cancelled) setProducts(Array.isArray(r.data) ? r.data : []);
    }).catch(() => {
      if (!cancelled) setProducts([]);
    }).finally(() => {
      if (!cancelled) setLoading(false);
    });
    return () => { cancelled = true; };
  }, []);

  // When a product is picked, pull its stock rows across variants.
  React.useEffect(() => {
    if (!line.accessory_product_id) { setStock([]); return; }
    let cancelled = false;
    axios.get(`${API}/ha/accessory-stock`, {
      params: { product_id: line.accessory_product_id },
    }).then((r) => {
      if (!cancelled) setStock(Array.isArray(r.data) ? r.data : []);
    }).catch(() => {
      if (!cancelled) setStock([]);
    });
    return () => { cancelled = true; };
  }, [line.accessory_product_id]);

  const pickedProduct = products.find((p) => p.product_id === line.accessory_product_id) || null;
  const variantLabels = pickedProduct?.variant_labels || [];
  // Aggregate stock qty per variant across branches (audiologists can sell
  // from any branch they have visibility into; the paid-invoice hook decrements
  // the actor's branch row specifically at payment time).
  const stockByVariant = React.useMemo(() => {
    const m = new Map();
    for (const s of stock) {
      const v = s.variant || '';
      m.set(v, (m.get(v) || 0) + Number(s.qty_on_hand || 0));
    }
    return m;
  }, [stock]);

  const onPickProduct = (product_id) => {
    if (!product_id) {
      onChange({ accessory_product_id: null, accessory_variant: null });
      return;
    }
    const p = products.find((x) => x.product_id === product_id);
    if (!p) return;
    // Auto-fill display fields; the user can still edit them for one-off pricing.
    const nextVariant = (p.variant_labels || []).length === 1 ? p.variant_labels[0] : null;
    onChange({
      accessory_product_id: p.product_id,
      accessory_variant: nextVariant,
      make: p.brand || '',
      model: p.model || '',
      unit_price: Number(p.mrp || 0) || Number(line.unit_price || 0),
      gst_rate: Number(p.gst_rate ?? line.gst_rate ?? 18),
    });
  };

  const onPickVariant = (variant) => onChange({ accessory_variant: variant || null });

  const pickedVariantStock = line.accessory_variant
    ? stockByVariant.get(line.accessory_variant)
    : (pickedProduct && variantLabels.length === 0 ? stockByVariant.get('') : undefined);

  const invoiceQty = Math.max(1, Math.floor(Number(line.quantity) || 1));

  return (
    <div
      className="rounded border border-teal-200 bg-teal-50/60 px-2 py-2 space-y-2"
      data-testid={`ci-accessory-picker-${line.key}`}
    >
      <div className="flex items-center gap-2 flex-wrap">
        <span className="text-[10px] font-bold uppercase tracking-wider text-teal-700">
          Accessory picker
        </span>
        <span className="text-[10px] text-slate-500 italic">
          Optional — attaches the exact SKU + variant so stock decrements automatically on payment
        </span>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-[1fr_180px_120px] gap-2">
        <FieldLabel label="Accessory SKU">
          <select
            value={line.accessory_product_id || ''}
            onChange={(e) => onPickProduct(e.target.value)}
            disabled={loading || products.length === 0}
            data-testid={`ci-accessory-sku-${line.key}`}
            className="w-full px-2 py-1 text-xs border border-slate-300 rounded bg-white disabled:bg-slate-100"
          >
            <option value="">
              {loading ? 'Loading catalogue…' : products.length === 0 ? 'No accessory SKUs in catalogue' : '— pick from catalogue —'}
            </option>
            {products.map((p) => (
              <option key={p.product_id} value={p.product_id}>
                {p.brand} · {p.model}{p.accessory_kind ? ` (${p.accessory_kind.replace('_', ' ')})` : ''}
              </option>
            ))}
          </select>
        </FieldLabel>

        <FieldLabel
          label="Variant / Size"
          disabled={!pickedProduct || variantLabels.length === 0}
          hint={pickedProduct && variantLabels.length === 0 ? 'No sizes' : undefined}
        >
          <select
            value={line.accessory_variant || ''}
            onChange={(e) => onPickVariant(e.target.value)}
            disabled={!pickedProduct || variantLabels.length === 0}
            data-testid={`ci-accessory-variant-${line.key}`}
            className="w-full px-2 py-1 text-xs border border-slate-300 rounded bg-white disabled:bg-slate-100 disabled:text-slate-400"
          >
            <option value="">— pick —</option>
            {variantLabels.map((v) => {
              const q = stockByVariant.get(v);
              // Concatenate as a plain string so we never render a <span>
              // inside <option> (invalid HTML → React hydration warning).
              const marker = q == null ? '' : q === 0 ? ' · OUT' : ` · ${q} on hand`;
              return <option key={v} value={v}>{`${v}${marker}`}</option>;
            })}
          </select>
        </FieldLabel>

        <FieldLabel label="In stock" hint="Across branches">
          <div
            data-testid={`ci-accessory-stock-${line.key}`}
            className={`w-full px-2 py-1 text-xs border rounded text-center font-bold tabular-nums ${
              pickedVariantStock == null
                ? 'border-slate-200 bg-slate-50 text-slate-400'
                : pickedVariantStock === 0
                  ? 'border-rose-300 bg-rose-50 text-rose-700'
                  : pickedVariantStock < invoiceQty
                    ? 'border-amber-300 bg-amber-50 text-amber-800'
                    : 'border-emerald-300 bg-emerald-50 text-emerald-700'
            }`}
          >
            {pickedVariantStock == null ? '—' : pickedVariantStock}
          </div>
        </FieldLabel>
      </div>

      {/* Advisory warnings — never block save (audiologist may have stock in transit). */}
      {pickedProduct && line.accessory_variant && pickedVariantStock === 0 && (
        <div className="text-[11px] text-rose-700 bg-white border border-rose-200 rounded px-2 py-1"
             data-testid={`ci-accessory-warn-out-${line.key}`}>
          <b>Heads up:</b> this variant is out of stock in the catalogue. The invoice will still save, but the auto-decrement will floor to zero (a shortfall gets logged).
        </div>
      )}
      {pickedProduct && line.accessory_variant && pickedVariantStock != null &&
        pickedVariantStock > 0 && pickedVariantStock < invoiceQty && (
        <div className="text-[11px] text-amber-800 bg-white border border-amber-200 rounded px-2 py-1"
             data-testid={`ci-accessory-warn-low-${line.key}`}>
          <b>Low stock:</b> only {pickedVariantStock} on hand, invoice asks for {invoiceQty}.
        </div>
      )}
    </div>
  );
}
