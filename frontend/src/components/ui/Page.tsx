import { cn } from "../../lib/utils";

export function Page({
  children,
  className,
}: {
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <div
      className={cn(
        "flex-1 flex flex-col min-h-0 bg-surface",
        className
      )}
    >
      {children}
    </div>
  );
}
