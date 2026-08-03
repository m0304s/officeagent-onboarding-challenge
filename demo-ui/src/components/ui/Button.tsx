import type { ButtonHTMLAttributes, ReactNode } from "react";

import styles from "./Button.module.css";

type Variant = "primary" | "secondary" | "danger" | "ghost";

interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: Variant;
  loading?: boolean;
  invalid?: boolean;
  children: ReactNode;
}

const VARIANT_CLASS: Record<Variant, string | undefined> = {
  primary: styles.primary,
  secondary: undefined,
  danger: styles.danger,
  ghost: styles.ghost,
};

export function Button({
  variant = "secondary",
  loading = false,
  invalid = false,
  disabled,
  children,
  ...rest
}: ButtonProps) {
  const className = [
    styles.button,
    VARIANT_CLASS[variant],
    loading ? styles.loading : undefined,
    invalid ? styles.invalid : undefined,
    rest.className,
  ]
    .filter(Boolean)
    .join(" ");

  return (
    <button
      {...rest}
      className={className}
      disabled={disabled || loading}
      aria-busy={loading || undefined}
      aria-invalid={invalid || undefined}
    >
      {loading ? <span className={styles.spinner} aria-hidden="true" /> : null}
      {children}
    </button>
  );
}
