import React from 'react';
import { NavLink, Routes, Route, Navigate, useLocation } from 'react-router-dom';
import InvoicesListPage from './InvoicesListPage';
import InvoiceDetailPage from './InvoiceDetailPage';
import CreateInvoicePage from './CreateInvoicePage';
import PaymentsRefundsPage from './PaymentsRefundsPage';
import ServiceCatalogPage from './ServiceCatalogPage';
import MySubscriptionPage from './MySubscriptionPage';
import AdvanceReceiptsPage from './AdvanceReceiptsPage';
import { useAuth } from '../../AuthContext';

const Tab = ({ to, label, testid }) => {
  const loc = useLocation();
  // Mark /billing (index) active only when on /billing exactly or /billing/invoices*
  const isIndex = to === '/billing';
  const active = isIndex
    ? (loc.pathname === '/billing' || loc.pathname.startsWith('/billing/invoice'))
    : loc.pathname.startsWith(to);
  return (
    <NavLink
      to={to}
      data-testid={testid}
      className={`px-3 py-1.5 text-xs font-semibold rounded transition-colors ${
        active ? 'bg-emerald-600 text-white shadow-sm' : 'text-slate-600 hover:bg-slate-100'
      }`}
    >
      {label}
    </NavLink>
  );
};

const CatalogGate = ({ canManageCatalog, children }) =>
  (canManageCatalog ? children : <Navigate to="/billing" replace />);

export default function BillingModule() {
  const { user } = useAuth();
  const canManageCatalog = ['super_admin', 'founder', 'clinic_owner', 'accounts'].includes(user?.role);
  const canSeeSubscription = ['super_admin', 'founder', 'clinic_owner'].includes(user?.role);

  return (
    <div className="h-full flex flex-col" data-testid="billing-module">
      <div className="bg-white border-b border-slate-200 px-4 py-2 flex items-center gap-2 flex-shrink-0 overflow-x-auto">
        <h2 className="text-sm font-bold text-slate-800 mr-3 shrink-0">Billing</h2>
        <Tab to="/billing" testid="bill-tab-invoices" label="Invoices" />
        <Tab to="/billing/new" testid="bill-tab-new" label="+ New Invoice" />
        <Tab to="/billing/advances" testid="bill-tab-advances" label="Advances" />
        <Tab to="/billing/payments" testid="bill-tab-payments" label="Payments & Refunds" />
        {canManageCatalog && <Tab to="/billing/catalog" testid="bill-tab-catalog" label="Service Catalog" />}
        {canSeeSubscription && <Tab to="/billing/my-subscription" testid="bill-tab-my-sub" label="My Subscription" />}
      </div>

      <div className="flex-1 overflow-auto">
        <Routes>
          <Route index element={<InvoicesListPage />} />
          <Route path="new" element={<CreateInvoicePage />} />
          <Route path="advances" element={<AdvanceReceiptsPage />} />
          <Route path="payments" element={<PaymentsRefundsPage />} />
          <Route path="invoice/:invoiceId" element={<InvoiceDetailPage />} />
          <Route
            path="catalog"
            element={<CatalogGate canManageCatalog={canManageCatalog}><ServiceCatalogPage /></CatalogGate>}
          />
          <Route path="my-subscription" element={<MySubscriptionPage />} />
          <Route path="*" element={<Navigate to="." replace />} />
        </Routes>
      </div>
    </div>
  );
}
