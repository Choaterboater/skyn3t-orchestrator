import type { ReactNode } from "react";

export function PanelCard({
  children,
  className = "",
}: {
  children: ReactNode;
  className?: string;
}) {
  return <section className={["panel-card", className].join(" ")}>{children}</section>;
}

export function PanelHeader({
  title,
  icon,
  description,
  actions,
}: {
  title: string;
  icon?: string;
  description?: ReactNode;
  actions?: ReactNode;
}) {
  return (
    <header className="panel-header">
      <div className="min-w-0">
        <h2 className="display text-lg text-text-primary flex items-center gap-2">
          {icon && <i className={[icon, "text-accent text-base not-italic"].join(" ")} />}
          <span className="text-accent">{title}</span>
        </h2>
        {description && (
          <p className="text-text-secondary text-sm mt-1 max-w-2xl">{description}</p>
        )}
      </div>
      {actions && <div className="shrink-0">{actions}</div>}
    </header>
  );
}

export function PageHeader({
  title,
  subtitle,
  aside,
}: {
  title: string;
  subtitle?: string;
  aside?: ReactNode;
}) {
  return (
    <header className="flex flex-wrap items-end justify-between gap-4">
      <div>
        <h1 className="display text-4xl">
          <span className="text-accent">{title}</span>
        </h1>
        {subtitle && <p className="text-text-secondary text-sm mt-1 max-w-xl">{subtitle}</p>}
      </div>
      {aside}
    </header>
  );
}
