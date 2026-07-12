import { Search } from 'lucide-react';
import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { Input } from '@/components/ui/input';

export function GlobalSearch() {
  const [value, setValue] = useState('');
  const navigate = useNavigate();

  return (
    <form
      role="search"
      className="relative hidden lg:block"
      onSubmit={(e) => {
        e.preventDefault();
        const q = value.trim();
        navigate(q ? `/opportunities?search=${encodeURIComponent(q)}` : '/opportunities');
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
        className="h-9 w-56 pl-8"
      />
    </form>
  );
}
