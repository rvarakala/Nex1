/**
 * PatientAdvancesBanner — Phase 2B.3 (UX)
 * ---------------------------------------
 * Informational-only banner shown on sale/order forms (HA Sale,
 * Custom HA, Ear Mould, Accessory) alerting staff to any existing
 * Advance Receipts the picked patient still has available. Clicking
 * the banner deep-links to the Advance Receipts screen filtered to
 * that patient — where staff can click "Apply Advance" and pick the
 * freshly-created invoice.
 *
 * DELIBERATELY informational only: this component NEVER modifies
 * financial state and NEVER calls the allocation endpoint. It is a
 * pure discovery aid. The Phase 2B.2 allocation endpoint remains the
 * single authoritative writer.
 *
 * Why not auto-apply post-sale?
 *   • Sale creation + allocation are two separate financial writes.
 *     Chaining them client-side introduces a "half-applied" failure
 *     window that this staff-in-the-loop pattern deliberately avoids.
 *   • Explicit staff confirmation preserves auditability.
 */
import React, { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { IndianRupee, ExternalLink } from 'lucide-react';
import axios from 'axios';

const API = `${process.env.REACT_APP_BACKEND_URL}/api`;

const fmtINR = (n) => `₹${Number(n || 0).toLocaleString('en-IN', { maximumFractionDigits: 2, minimumFractionDigits: 2 })}`;

/**
 * Props:
 *   patientId?: str          — patient scope; renders nothing if falsy
 *   className?: str          — extra layout hooks
 */
export default function PatientAdvancesBanner({ patientId, className = '' }) {
  const [state, setState] = useState({ loading: false, available: 0, count: 0 });

  useEffect(() => {
    if (!patientId) { setState({ loading: false, available: 0, count: 0 }); return; }
    let cancelled = false;
    (async () => {
      setState((s) => ({ ...s, loading: true }));
      try {
        const r = await axios.get(`${API}/advance-receipts`, {
          params: { patient_id: patientId, status: 'active', limit: 100 },
        });
        const items = r.data?.items || [];
        // Sum available_balance across active receipts. Legacy Phase
        // 2A rows are skipped — their balance is not queryable until
        // the controlled backfill runs.
        let total = 0;
        let counted = 0;
        for (const it of items) {
          const v = it?.available_balance;
          if (v === null || v === undefined) continue;
          if (Number(v) > 0.01) {
            total += Number(v);
            counted += 1;
          }
        }
        if (!cancelled) setState({ loading: false, available: total, count: counted });
      } catch {
        if (!cancelled) setState({ loading: false, available: 0, count: 0 });
      }
    })();
    return () => { cancelled = true; };
  }, [patientId]);

  if (!patientId || state.loading || state.count === 0 || state.available <= 0.01) return null;

  return (
    <Link
      to={`/billing/advances?patient=${encodeURIComponent(patientId)}`}
      target="_blank"
      rel="noopener noreferrer"
      className={`flex items-center gap-2 px-3 py-2 rounded-lg border border-emerald-300 bg-emerald-50 hover:bg-emerald-100 transition text-emerald-900 text-[12px] ${className}`}
      data-testid="patient-advances-banner"
      title="Open Advance Receipts page to apply this advance to the new invoice"
    >
      <IndianRupee size={14} className="text-emerald-700 shrink-0" />
      <div className="flex-1">
        <div className="font-semibold">
          {fmtINR(state.available)} available in existing advance{state.count > 1 ? `s` : ''}
        </div>
        <div className="text-[10.5px] text-emerald-700/80">
          After creating this sale, open the Advance Receipts screen and click <b>Apply Advance</b> to apply against the new invoice.
        </div>
      </div>
      <ExternalLink size={12} className="text-emerald-700/70 shrink-0" />
    </Link>
  );
}
