import { useId, useState, type FormEvent } from "react";
import { AlertDialog, AlertDialogCancel, AlertDialogContent, AlertDialogDescription, AlertDialogTitle } from "@/components/ui/alert-dialog";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { useI18n } from "@/i18n";

interface PlaylistNameDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: string;
  confirmLabel: string;
  /** The name to start from (a rename). */
  initialName?: string;
  /** The name, trimmed and never empty. */
  onConfirm: (name: string) => void;
}

/** The most the server keeps of a playlist's name (`PlaylistBody`). */
const NAME_MAX = 200;

/** Asks for the name of a playlist: a new one, or the new name of one. */
export function PlaylistNameDialog({ open, onOpenChange, title, confirmLabel, initialName = "", onConfirm }: PlaylistNameDialogProps) {
  return (
    <AlertDialog open={open} onOpenChange={onOpenChange}>
      {/* Mounted only while open: every opening starts from the initial name. */}
      <AlertDialogContent>
        <NameForm title={title} confirmLabel={confirmLabel} initialName={initialName} onConfirm={onConfirm} onClose={() => onOpenChange(false)} />
      </AlertDialogContent>
    </AlertDialog>
  );
}

function NameForm({ title, confirmLabel, initialName, onConfirm, onClose }: Pick<PlaylistNameDialogProps, "title" | "confirmLabel" | "onConfirm"> & { initialName: string; onClose: () => void }) {
  const { t } = useI18n();
  const [name, setName] = useState(initialName);
  const inputId = useId();
  const trimmed = name.trim();

  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (!trimmed) return;
    onConfirm(trimmed);
    onClose();
  };

  return (
    <form onSubmit={submit} className="grid gap-4">
      <AlertDialogTitle>{title}</AlertDialogTitle>
      <AlertDialogDescription className="sr-only">{title}</AlertDialogDescription>
      <div className="space-y-1.5">
        <label htmlFor={inputId} className="text-sm font-medium">
          {t("playlists.nameLabel")}
        </label>
        <Input
          id={inputId}
          value={name}
          maxLength={NAME_MAX}
          autoFocus
          placeholder={t("playlists.namePlaceholder")}
          onChange={(e) => setName(e.target.value)}
          className="h-11 sm:h-9"
        />
      </div>
      <div className="flex flex-col-reverse gap-2 sm:flex-row sm:justify-end">
        <AlertDialogCancel type="button">{t("common.cancel")}</AlertDialogCancel>
        <Button type="submit" disabled={!trimmed}>
          {confirmLabel}
        </Button>
      </div>
    </form>
  );
}
