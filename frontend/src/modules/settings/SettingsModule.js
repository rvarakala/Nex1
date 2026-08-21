/**
 * Settings Module — clinic-admin only.
 *
 * On desktop the sidebar sits fixed on the left. On phones (< 768px) the
 * sidebar collapses behind a "Menu ▼" button that opens a full-height
 * drawer. Picking any tab auto-closes the drawer so the content takes the
 * full viewport width.
 */
import React, { useEffect, useMemo, useState } from 'react';
import { NavLink, Route, Routes, Navigate, useLocation } from 'react-router-dom';
import {
  Settings, Building2, Users, MapPin, Pen, ListChecks, ShieldCheck,
  MessageCircle, Clock, CalendarClock, Upload, User, Printer, Stamp,
  Stethoscope, Menu, X, ChevronDown, Crown, Puzzle,
} from 'lucide-react';
import ClinicDetailsTab from './ClinicDetailsTab';
import StaffSettingsTab from './StaffSettingsTab';
import BranchesTab from './BranchesTab';
import ClinicGroupTab from './ClinicGroupTab';
import MySignatureTab from './MySignatureTab';
import MySealTab from './MySealTab';
import MyProfileTab from './MyProfileTab';
import SecurityPrivacyTab from './SecurityPrivacyTab';
import ConnectWhatsAppTab from './ConnectWhatsAppTab';
import SettingsIntegrationsTab from './SettingsIntegrationsTab';
import ClinicHoursTab from './ClinicHoursTab';
import StaffScheduleTab from './StaffScheduleTab';
import DataImportTab from './DataImportTab';
import PrintTemplatesTab from './PrintTemplatesTab';
import BlankAudiogramTemplate from './templates/BlankAudiogramTemplate';
import ServiceCatalogPage from '../billing/ServiceCatalogPage';
import ReferralDoctorsTab from './ReferralDoctorsTab';
import { useAuth } from '../../AuthContext';

export default function SettingsModule() {
  const { user } = useAuth();
  const location = useLocation();
  const [drawerOpen, setDrawerOpen] = useState(false);

  const isAdmin = ['clinic_owner', 'super_admin'].includes(user?.role);
  const canManageCatalog = ['clinic_owner', 'super_admin', 'accounts'].includes(user?.role);

  // Auto-close mobile drawer whenever the route changes.
  useEffect(() => { setDrawerOpen(false); }, [location.pathname]);

  // Build the visible-to-this-user nav list. Central definition so both the
  // sidebar and the mobile drawer read from the same source of truth (and
  // the mobile top bar can look up the current tab's label).
  const navItems = useMemo(() => {
    const items = [];
    if (isAdmin) {
      items.push(
        { to: '/settings/clinic',           icon: Building2,     label: 'Clinic Details',    testid: 'settings-nav-clinic' },
        { to: '/settings/hours',            icon: Clock,         label: 'Clinic Hours',      testid: 'settings-nav-hours' },
        { to: '/settings/staff',            icon: Users,         label: 'Staff Settings',    testid: 'settings-nav-staff' },
        { to: '/settings/staff-schedule',   icon: CalendarClock, label: 'Doctor Schedule',   testid: 'settings-nav-staff-schedule' },
        { to: '/settings/referral-doctors', icon: Stethoscope,   label: 'Referral Doctors',  testid: 'settings-nav-referral-doctors' },
        { to: '/settings/branches',         icon: MapPin,        label: 'Branches',          testid: 'settings-nav-branches' },
        { to: '/settings/clinic-group',     icon: Crown,         label: 'Clinic Group',      testid: 'settings-nav-clinic-group' },
        { to: '/settings/security',         icon: ShieldCheck,   label: 'Security & Privacy',testid: 'settings-nav-security' },
        { to: '/settings/connect',          icon: MessageCircle, label: 'Connect (WhatsApp)',testid: 'settings-nav-connect' },
        { to: '/settings/integrations',     icon: Puzzle,        label: 'Integrations',      testid: 'settings-nav-integrations' },
        { to: '/settings/import',           icon: Upload,        label: 'Data Import',       testid: 'settings-nav-import' },
        { to: '/settings/templates',        icon: Printer,       label: 'Print Templates',   testid: 'settings-nav-templates' },
      );
    }
    if (canManageCatalog) {
      items.push({ to: '/settings/services', icon: ListChecks, label: 'Services & Packages', testid: 'settings-nav-services' });
    }
    // Divider marker — rendered as a horizontal rule when present.
    if (items.length > 0) items.push({ divider: true });
    items.push(
      { to: '/settings/profile',   icon: User,  label: 'My Profile',   testid: 'settings-nav-profile' },
      { to: '/settings/signature', icon: Pen,   label: 'My Signature', testid: 'settings-nav-signature' },
      { to: '/settings/seal',      icon: Stamp, label: 'My Seal',      testid: 'settings-nav-seal' },
    );
    return items;
  }, [isAdmin, canManageCatalog]);

  // Label for the mobile top bar — mirrors the currently active tab.
  const activeTab = navItems.find((it) => !it.divider && location.pathname.startsWith(it.to));
  const activeLabel = activeTab?.label || 'Settings';
  const ActiveIcon = activeTab?.icon || Settings;

  return (
    <div className="h-full flex flex-col md:flex-row bg-slate-50" data-testid="settings-module">
      {/* Mobile top bar — visible on < md */}
      <div className="md:hidden sticky top-0 z-20 flex items-center justify-between gap-2 bg-white border-b border-slate-200 px-3 py-2">
        <button
          onClick={() => setDrawerOpen(true)}
          data-testid="settings-mobile-menu-toggle"
          className="flex items-center gap-2 flex-1 min-w-0 px-2 py-1.5 text-sm font-semibold text-slate-700 bg-slate-50 border border-slate-200 rounded hover:bg-slate-100 active:bg-slate-200"
        >
          <Menu size={14} />
          <ActiveIcon size={14} className="text-indigo-600 shrink-0" />
          <span className="truncate flex-1 text-left">{activeLabel}</span>
          <ChevronDown size={14} className="text-slate-400 shrink-0" />
        </button>
      </div>

      {/* Desktop sidebar — hidden on < md */}
      <aside className="hidden md:flex w-56 bg-white border-r border-slate-200 p-3 flex-col shrink-0">
        <div className="flex items-center gap-2 px-2 py-1 mb-3">
          <Settings size={16} className="text-slate-500" />
          <div className="text-[11px] uppercase tracking-wider font-bold text-slate-500">Settings</div>
        </div>
        <NavList items={navItems} />
      </aside>

      {/* Mobile drawer — full-height slide-in from the left */}
      {drawerOpen && (
        <div className="md:hidden fixed inset-0 z-50 flex" data-testid="settings-mobile-drawer">
          {/* Backdrop */}
          <button
            aria-label="Close menu"
            onClick={() => setDrawerOpen(false)}
            className="absolute inset-0 bg-slate-900/50"
          />
          {/* Drawer */}
          <div className="relative w-72 max-w-[85vw] bg-white h-full shadow-xl flex flex-col animate-in slide-in-from-left duration-150">
            <div className="flex items-center justify-between px-4 py-3 border-b border-slate-200">
              <div className="flex items-center gap-2">
                <Settings size={16} className="text-slate-500" />
                <div className="text-[11px] uppercase tracking-wider font-bold text-slate-500">Settings</div>
              </div>
              <button
                onClick={() => setDrawerOpen(false)}
                data-testid="settings-mobile-drawer-close"
                className="p-1 hover:bg-slate-100 rounded"
              >
                <X size={16} className="text-slate-500" />
              </button>
            </div>
            <div className="flex-1 overflow-y-auto p-3">
              <NavList items={navItems} />
            </div>
          </div>
        </div>
      )}

      {/* Content area — full width on mobile, remaining space on desktop */}
      <main className="flex-1 overflow-auto min-w-0">
        <Routes>
          <Route index element={<Navigate to={isAdmin ? 'clinic' : (canManageCatalog ? 'services' : 'profile')} replace />} />
          {isAdmin && <Route path="clinic"   element={<ClinicDetailsTab />} />}
          {isAdmin && <Route path="hours"    element={<ClinicHoursTab />} />}
          {isAdmin && <Route path="staff"    element={<StaffSettingsTab />} />}
          {isAdmin && <Route path="staff-schedule" element={<StaffScheduleTab />} />}
          {isAdmin && <Route path="referral-doctors" element={<ReferralDoctorsTab />} />}
          {isAdmin && <Route path="branches" element={<BranchesTab />} />}
          {isAdmin && <Route path="clinic-group" element={<ClinicGroupTab />} />}
          {isAdmin && <Route path="security" element={<SecurityPrivacyTab />} />}
          {isAdmin && <Route path="connect"  element={<ConnectWhatsAppTab />} />}
          {isAdmin && <Route path="integrations" element={<SettingsIntegrationsTab />} />}
          {isAdmin && <Route path="import"   element={<DataImportTab />} />}
          {isAdmin && <Route path="templates" element={<PrintTemplatesTab />} />}
          {isAdmin && <Route path="templates/audiogram" element={<BlankAudiogramTemplate />} />}
          {canManageCatalog && <Route path="services" element={<ServiceCatalogPage />} />}
          <Route path="profile"   element={<MyProfileTab />} />
          <Route path="signature" element={<MySignatureTab />} />
          <Route path="seal"      element={<MySealTab />} />
        </Routes>
      </main>
    </div>
  );
}

function NavList({ items }) {
  return (
    <nav className="flex flex-col">
      {items.map((it, i) => {
        if (it.divider) return <div key={`div-${i}`} className="my-2 border-t border-slate-100" />;
        return (
          <SideLink key={it.to} to={it.to} icon={<it.icon size={14} />} label={it.label} testid={it.testid} />
        );
      })}
    </nav>
  );
}

function SideLink({ to, icon, label, testid }) {
  return (
    <NavLink
      to={to}
      data-testid={testid}
      className={({ isActive }) =>
        `flex items-center gap-2 px-3 py-2 text-xs font-semibold rounded transition ${
          isActive ? 'bg-indigo-50 text-indigo-700 border border-indigo-200' : 'text-slate-600 hover:bg-slate-50'
        }`
      }
    >
      {icon}
      {label}
    </NavLink>
  );
}
