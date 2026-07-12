import * as ToastPrimitive from '@radix-ui/react-toast';
import { X } from 'lucide-react';
import { createContext, useCallback, useContext, useMemo, useState } from 'react';
import { cn } from '@/lib/utils';

type ToastIntent = 'default' | 'success' | 'error' | 'warning';

interface ToastItem {
  id: string;
  title: string;
  description?: string;
  intent: ToastIntent;
}

interface ToastContextValue {
  toast: (input: { title: string; description?: string; intent?: ToastIntent }) => void;
}

const ToastContext = createContext<ToastContextValue | null>(null);

const intentStyles: Record<ToastIntent, string> = {
  default: 'border-border bg-card',
  success: 'border-success/40 bg-card',
  error: 'border-destructive/50 bg-card',
  warning: 'border-warning/40 bg-card',
};

const intentBar: Record<ToastIntent, string> = {
  default: 'bg-primary',
  success: 'bg-success',
  error: 'bg-destructive',
  warning: 'bg-warning',
};

export function ToastProvider({ children }: { children: React.ReactNode }) {
  const [toasts, setToasts] = useState<ToastItem[]>([]);

  const toast = useCallback<ToastContextValue['toast']>(({ title, description, intent = 'default' }) => {
    const id = `${Date.now()}-${Math.random().toString(16).slice(2)}`;
    setToasts((current) => [...current, { id, title, description, intent }]);
  }, []);

  const remove = useCallback((id: string) => {
    setToasts((current) => current.filter((t) => t.id !== id));
  }, []);

  const value = useMemo(() => ({ toast }), [toast]);

  return (
    <ToastContext.Provider value={value}>
      <ToastPrimitive.Provider swipeDirection="right" duration={5000}>
        {children}
        {toasts.map((item) => (
          <ToastPrimitive.Root
            key={item.id}
            onOpenChange={(open) => {
              if (!open) remove(item.id);
            }}
            className={cn(
              'relative flex items-start gap-3 overflow-hidden rounded-md border p-4 pr-8 shadow-lg',
              intentStyles[item.intent],
            )}
          >
            <span className={cn('absolute inset-y-0 left-0 w-1', intentBar[item.intent])} aria-hidden />
            <div className="grid gap-0.5">
              <ToastPrimitive.Title className="text-sm font-semibold text-foreground">
                {item.title}
              </ToastPrimitive.Title>
              {item.description ? (
                <ToastPrimitive.Description className="text-sm text-muted-foreground">
                  {item.description}
                </ToastPrimitive.Description>
              ) : null}
            </div>
            <ToastPrimitive.Close
              className="absolute right-2 top-2 rounded text-muted-foreground opacity-70 hover:opacity-100 focus:outline-none focus:ring-2 focus:ring-ring"
              aria-label="Dismiss"
            >
              <X className="size-4" />
            </ToastPrimitive.Close>
          </ToastPrimitive.Root>
        ))}
        <ToastPrimitive.Viewport className="fixed bottom-0 right-0 z-[100] flex max-h-screen w-full flex-col gap-2 p-4 sm:max-w-sm" />
      </ToastPrimitive.Provider>
    </ToastContext.Provider>
  );
}

export function useToast(): ToastContextValue {
  const ctx = useContext(ToastContext);
  if (!ctx) throw new Error('useToast must be used within ToastProvider');
  return ctx;
}
