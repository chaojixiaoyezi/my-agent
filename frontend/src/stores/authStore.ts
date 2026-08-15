import { create } from "zustand";

type UserRole = "admin" | "user";

const SENSITIVE_FIELDS = new Set([
  "workspace_root",
  "api_key",
  "api_base",
  "model_backend",
  "tool_agent_budget_window_seconds",
  "tool_agent_budget_max_calls",
]);

const ADMIN_ONLY_PAGES = new Set(["/settings"]);

const DEFAULT_ROLE: UserRole = "admin";

export type AuthState = {
  role: UserRole;
  isAuthenticated: boolean;
  isAdmin: boolean;
  login: (role: UserRole) => void;
  logout: () => void;
  canEditField: (fieldKey: string) => boolean;
  canAccessPage: (pathname: string) => boolean;
};

export const useAuthStore = create<AuthState>((set, get) => ({
  role: DEFAULT_ROLE,
  isAuthenticated: true,
  isAdmin: DEFAULT_ROLE === "admin",

  login: (role) =>
    set((state) => ({
      ...state,
      role,
      isAuthenticated: true,
      isAdmin: role === "admin",
    })),

  logout: () =>
    set((state) => ({
      ...state,
      role: "user",
      isAuthenticated: false,
      isAdmin: false,
    })),

  canEditField: (fieldKey) => {
    const { isAdmin } = get();
    if (isAdmin) return true;
    return !SENSITIVE_FIELDS.has(fieldKey);
  },

  canAccessPage: (pathname) => {
    const { isAdmin } = get();
    if (isAdmin) return true;
    return !ADMIN_ONLY_PAGES.has(pathname);
  },
}));
