import { Search } from 'lucide-react';
import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { Button } from '@/components/ui/button';
import { Dialog, DialogContent, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { Input } from '@/components/ui/input';
import { cn } from '@/lib/utils';

/** The opportunity feed for a submitted term; an empty term opens it unfiltered. */
function searchPath(value: string): string {
  const q = value.trim();
  return q ? `/opportunities?search=${encodeURIComponent(q)}` : '/opportunities';
}

function SearchForm({ className, onSearched }: { className?: string; onSearched?: () => void }) {
  const [value, setValue] = useState('');
  const navigate = useNavigate();

  return (
    <form
      role="search"
      className={cn('relative', className)}
      onSubmit={(e) => {
        e.preventDefault();
        navigate(searchPath(value));
        onSearched?.();
      }}
    >
      <Search
        className="pointer-events-none absolute left-2.5 top-1/2 size-4 -translate-y-1/2 text-muted-foreground"
        aria-hidden
      />
      <Input
        type="search"
        value={value}
        onChange={(e) => setValue(e.target.value)}
        placeholder="Search opportunities…"
        aria-label="Search opportunities"
        className="h-9 w-full pl-8"
      />
    </form>
  );
}

// The header field from 1024px; below that there is no room for it, so a button opens
// the same search in a dialog (P6-UI-015).
export function GlobalSearch() {
  const [open, setOpen] = useState(false);

  return (
    <>
      <SearchForm className="hidden w-56 lg:block" />
      <Button
        variant="ghost"
        size="icon"
        className="lg:hidden"
        aria-label="Search opportunities"
        onClick={() => setOpen(true)}
      >
        <Search className="size-4" />
      </Button>
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent className="max-w-md" aria-describedby={undefined}>
          <DialogHeader>
            <DialogTitle>Search opportunities</DialogTitle>
          </DialogHeader>
          {/* Mounted only while open: each opening starts from an empty field. */}
          <SearchForm onSearched={() => setOpen(false)} />
        </DialogContent>
      </Dialog>
    </>
  );
}
