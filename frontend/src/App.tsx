import { Routes, Route } from "react-router-dom";
import { useEffect, useState } from "react";
import { Sidebar } from "./components/ui/Sidebar";
import { Header } from "./components/ui/Header";
import { Toast } from "./components/ui/Toast";
import { ErrorBoundary } from "./components/ui/ErrorBoundary";
import { RouteGuard } from "./components/settings/RouteGuard";
import { useConfigStore } from "./stores/configStore";
import Dashboard from "./pages/Dashboard";
import Config from "./pages/Config";
import Subagents from "./pages/Subagents";
import Memory from "./pages/Memory";
import Tools from "./pages/Tools";
import Logs from "./pages/Logs";
import Templates from "./pages/Templates";
import { SettingsLayout } from "./components/settings/SettingsLayout";
import SettingsOverview from "./pages/settings/SettingsOverview";
import SettingsDispatch from "./pages/settings/SettingsDispatch";
import SettingsModel from "./pages/settings/SettingsModel";
import SettingsTools from "./pages/settings/SettingsTools";
import SettingsMemory from "./pages/settings/SettingsMemory";
import SettingsGateway from "./pages/settings/SettingsGateway";
import SettingsSubagents from "./pages/settings/SettingsSubagents";
import SettingsSecurity from "./pages/settings/SettingsSecurity";
import SettingsAdapters from "./pages/settings/SettingsAdapters";

export default function App() {
  const toast = useConfigStore((s) => s.toast);
  const hideToast = useConfigStore((s) => s.hideToast);
  const init = useConfigStore((s) => s.init);
  const [sidebarOpen, setSidebarOpen] = useState(false);

  useEffect(() => {
    init();
  }, [init]);

  return (
    <div className="flex h-screen bg-surface overflow-hidden">
      <Sidebar open={sidebarOpen} onClose={() => setSidebarOpen(false)} />
      <div className="flex-1 flex flex-col min-w-0">
        <Header onMenuClick={() => setSidebarOpen(true)} />
        <main className="flex-1 overflow-y-auto p-6">
          <ErrorBoundary>
            <RouteGuard>
              <Routes>
                <Route path="/" element={<Dashboard />} />
                <Route path="/config" element={<Config />} />
                <Route path="/subagents" element={<Subagents />} />
                <Route path="/memory" element={<Memory />} />
                <Route path="/tools" element={<Tools />} />
                <Route path="/logs" element={<Logs />} />
                <Route path="/templates" element={<Templates />} />
                <Route path="/settings" element={<SettingsLayout />}>
                  <Route index element={<SettingsOverview />} />
                  <Route path="dispatch" element={<SettingsDispatch />} />
                  <Route path="model" element={<SettingsModel />} />
                  <Route path="tools" element={<SettingsTools />} />
                  <Route path="memory" element={<SettingsMemory />} />
                  <Route path="gateway" element={<SettingsGateway />} />
                  <Route path="subagents" element={<SettingsSubagents />} />
                  <Route path="security" element={<SettingsSecurity />} />
                  <Route path="adapters" element={<SettingsAdapters />} />
                </Route>
              </Routes>
            </RouteGuard>
          </ErrorBoundary>
        </main>
      </div>
      {toast?.visible && (
        <Toast message={toast.message} type={toast.type} onClose={hideToast} />
      )}
    </div>
  );
}
