import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { zodResolver } from '@hookform/resolvers/zod';
import { Plus } from 'lucide-react';
import { useState } from 'react';
import { useForm } from 'react-hook-form';
import { z } from 'zod';
import * as api from '@/api/endpoints';
import { queryKeys } from '@/api/queryKeys';
import { Field } from '@/components/common/form-field';
import { PageHeader } from '@/components/layout/page-header';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from '@/components/ui/dialog';
import { Input } from '@/components/ui/input';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { useToast } from '@/components/ui/toast';
import { useAuth } from '@/auth/AuthContext';
import { useTheme, type Theme } from '@/app/theme';
import { roleLabels } from '@/lib/labels';
import { useWorkspace } from '@/workspace/WorkspaceContext';

const wsSchema = z.object({ name: z.string().min(1, 'Workspace name is required') });

export function SettingsPage() {
  const { user, memberships } = useAuth();
  const { theme, setTheme } = useTheme();
  const { organizationId, activeOrganization, workspaces, activeWorkspace, setWorkspaceId } =
    useWorkspace();
  const queryClient = useQueryClient();
  const { toast } = useToast();
  const [open, setOpen] = useState(false);

  const isOperator = user?.is_operator ?? false;

  const runtime = useQuery({
    queryKey: queryKeys.runtimeSummary,
    queryFn: ({ signal }) => api.getRuntimeSummary(signal),
    staleTime: 60_000,
  });

  // The detailed backend topology is operator-only; ordinary customers only ever
  // see the coarse summary above and never request the internal endpoint.
  const runtimeDetail = useQuery({
    queryKey: queryKeys.runtimeDetail,
    queryFn: ({ signal }) => api.getRuntimeDetail(signal),
    staleTime: 60_000,
    enabled: isOperator,
  });

  const form = useForm<z.infer<typeof wsSchema>>({
    resolver: zodResolver(wsSchema),
    defaultValues: { name: '' },
  });

  const createWs = useMutation({
    mutationFn: (name: string) => api.createWorkspace(organizationId!, { name }),
    onSuccess: async (ws) => {
      await queryClient.invalidateQueries({ queryKey: queryKeys.workspaces(organizationId!) });
      setWorkspaceId(ws.id);
      toast({ title: 'Workspace created', intent: 'success' });
      setOpen(false);
      form.reset();
    },
    onError: (err) =>
      toast({
        title: 'Could not create workspace',
        description: err instanceof Error ? err.message : undefined,
        intent: 'error',
      }),
  });

  return (
    <div>
      <PageHeader title="Settings" description="Manage your account, workspaces and preferences." />

      <div className="grid gap-6 lg:grid-cols-2">
        <Card>
          <CardHeader>
            <CardTitle>Account</CardTitle>
          </CardHeader>
          <CardContent className="space-y-3 text-sm">
            <div className="flex justify-between">
              <span className="text-muted-foreground">Name</span>
              <span className="font-medium">{user?.full_name}</span>
            </div>
            <div className="flex justify-between">
              <span className="text-muted-foreground">Email</span>
              <span className="font-medium">{user?.email}</span>
            </div>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>Appearance</CardTitle>
          </CardHeader>
          <CardContent>
            <Field label="Theme" description="Choose how SignalNest looks on this device.">
              {({ id }) => (
                <Select value={theme} onValueChange={(v) => setTheme(v as Theme)}>
                  <SelectTrigger id={id} className="w-48">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="light">Light</SelectItem>
                    <SelectItem value="dark">Dark</SelectItem>
                    <SelectItem value="system">Match system</SelectItem>
                  </SelectContent>
                </Select>
              )}
            </Field>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>Organizations &amp; roles</CardTitle>
          </CardHeader>
          <CardContent className="space-y-2">
            {memberships.map((m) => (
              <div
                key={m.organization_id}
                className="flex items-center justify-between rounded-md border border-border px-3 py-2 text-sm"
              >
                <span className="font-medium">{m.organization_name}</span>
                <Badge intent="info">{roleLabels[m.role] ?? m.role}</Badge>
              </div>
            ))}
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="flex-row items-center justify-between space-y-0">
            <CardTitle>Workspaces</CardTitle>
            <Dialog open={open} onOpenChange={setOpen}>
              <DialogTrigger asChild>
                <Button size="sm" variant="outline">
                  <Plus className="size-4" /> New
                </Button>
              </DialogTrigger>
              <DialogContent className="max-w-sm">
                <DialogHeader>
                  <DialogTitle>Create workspace</DialogTitle>
                </DialogHeader>
                <form
                  id="create-ws"
                  onSubmit={form.handleSubmit((v) => createWs.mutate(v.name))}
                  className="space-y-3"
                >
                  <Field label="Workspace name" error={form.formState.errors.name?.message} required>
                    {({ id, describedBy, invalid }) => (
                      <Input id={id} aria-describedby={describedBy} aria-invalid={invalid} {...form.register('name')} />
                    )}
                  </Field>
                </form>
                <DialogFooter>
                  <Button variant="outline" onClick={() => setOpen(false)}>
                    Cancel
                  </Button>
                  <Button type="submit" form="create-ws" disabled={createWs.isPending}>
                    Create
                  </Button>
                </DialogFooter>
              </DialogContent>
            </Dialog>
          </CardHeader>
          <CardContent className="space-y-2">
            <p className="text-xs text-muted-foreground">
              {activeOrganization?.name} · {workspaces.length} workspace
              {workspaces.length === 1 ? '' : 's'}
            </p>
            {workspaces.map((ws) => (
              <div
                key={ws.id}
                className="flex items-center justify-between rounded-md border border-border px-3 py-2 text-sm"
              >
                <span className="font-medium">{ws.name}</span>
                {ws.id === activeWorkspace?.id ? (
                  <Badge intent="success">Active</Badge>
                ) : (
                  <Button size="sm" variant="ghost" onClick={() => setWorkspaceId(ws.id)}>
                    Switch
                  </Button>
                )}
              </div>
            ))}
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>Runtime</CardTitle>
          </CardHeader>
          <CardContent className="space-y-3 text-sm">
            {runtime.isLoading ? (
              <p className="text-muted-foreground">Loading runtime status…</p>
            ) : runtime.data ? (
              <>
                <div className="flex justify-between">
                  <span className="text-muted-foreground">Mode</span>
                  <Badge intent={runtime.data.is_local_mode ? 'info' : 'success'}>
                    {runtime.data.is_local_mode ? 'Local (zero-dependency)' : runtime.data.app_mode}
                  </Badge>
                </div>
                <div className="flex justify-between">
                  <span className="text-muted-foreground">Environment</span>
                  <span className="font-medium">{runtime.data.environment}</span>
                </div>
                <div className="flex justify-between">
                  <span className="text-muted-foreground">Backends</span>
                  <Badge intent={runtime.data.all_configured ? 'success' : 'danger'}>
                    {runtime.data.all_configured ? 'all ready' : 'attention needed'}
                  </Badge>
                </div>
                {isOperator && runtimeDetail.data ? (
                  <div className="space-y-1 pt-1">
                    <p className="text-xs font-medium text-muted-foreground">
                      Infrastructure (operator view)
                    </p>
                    {runtimeDetail.data.capabilities.map((cap) => (
                      <div
                        key={cap.name}
                        className="flex items-center justify-between rounded-md border border-border px-3 py-1.5"
                      >
                        <span className="font-medium capitalize">{cap.name}</span>
                        <span className="flex items-center gap-2">
                          <span className="text-muted-foreground">{cap.backend}</span>
                          <Badge intent={cap.configured ? 'success' : 'danger'}>
                            {cap.configured ? 'ready' : 'not configured'}
                          </Badge>
                        </span>
                      </div>
                    ))}
                  </div>
                ) : null}
              </>
            ) : (
              <p className="text-muted-foreground">Runtime status is unavailable.</p>
            )}
          </CardContent>
        </Card>
      </div>
    </div>
  );
}
