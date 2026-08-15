import clsx, { type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

export function classNames(...classes: (string | false | undefined)[]) {
  return classes.filter(Boolean).join(" ");
}

export function formatNumber(n: number): string {
  return n.toLocaleString("zh-CN");
}

export function formatDate(d: string | Date): string {
  const date = typeof d === "string" ? new Date(d) : d;
  return date.toLocaleString("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

export function debounce<T extends (...args: unknown[]) => void>(
  fn: T,
  ms = 300
) {
  let t: ReturnType<typeof setTimeout>;
  return (...args: Parameters<T>) => {
    clearTimeout(t);
    t = setTimeout(() => fn(...args), ms);
  };
}

export function objToYaml(obj: Record<string, unknown>, indent = 0): string {
  let s = "";
  const pad = "  ".repeat(indent);
  for (const [k, v] of Object.entries(obj)) {
    if (v === null || v === undefined) {
      s += `${pad}${k}: null\n`;
    } else if (typeof v === "boolean") {
      s += `${pad}${k}: ${v}\n`;
    } else if (typeof v === "number") {
      s += `${pad}${k}: ${v}\n`;
    } else if (typeof v === "string") {
      if (v.includes("\n") || v.includes(":")) {
        s += `${pad}${k}: |\n${v
          .split("\n")
          .map((l) => `${pad}  ${l}`)
          .join("\n")}\n`;
      } else {
        s += `${pad}${k}: "${v}"\n`;
      }
    } else if (Array.isArray(v)) {
      if (v.length === 0) {
        s += `${pad}${k}: []\n`;
      } else {
        s += `${pad}${k}:\n`;
        for (const item of v) {
          if (typeof item === "string") {
            s += `${pad}  - "${item}"\n`;
          } else {
            s += `${pad}  - ${item}\n`;
          }
        }
      }
    } else if (typeof v === "object") {
      s += `${pad}${k}:\n`;
      s += objToYaml(v as Record<string, unknown>, indent + 1);
    }
  }
  return s;
}
