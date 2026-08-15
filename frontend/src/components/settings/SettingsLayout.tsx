import { Outlet } from "react-router-dom";
import { SettingsNav } from "./SettingsNav";

export function SettingsLayout() {
  return (
    <div className="flex gap-6">
      <SettingsNav />
      <div className="flex-1 min-w-0">
        <Outlet />
      </div>
    </div>
  );
}
