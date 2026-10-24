import { useId, useState } from "react";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { Input } from "@/components/ui/input";
import { useI18n } from "@/i18n";

interface NoteDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: string;
  description: string;
  /** What the note is (it is optional: it may be left empty). */
  label: string;
  confirmLabel: string;
  /** Red confirm button, for an action that turns something down. */
  destructive?: boolean;
  /** The note, trimmed (empty when none was written). */
  onConfirm: (note: string) => void;
}

/** A confirmation with an optional short note (an approval's note, the reason of a denial). */
export function NoteDialog({ open, onOpenChange, title, description, label, confirmLabel, destructive, onConfirm }: NoteDialogProps) {
  return (
    <AlertDialog open={open} onOpenChange={onOpenChange}>
      {/* Mounted only while open: every opening starts with an empty note. */}
      <AlertDialogContent>
        <NoteForm title={title} description={description} label={label} confirmLabel={confirmLabel} destructive={destructive} onConfirm={onConfirm} />
      </AlertDialogContent>
    </AlertDialog>
  );
}

/** The most the server keeps of a note (`maxLength` of DownloadRequestApprove / DownloadRequestDeny). */
const NOTE_MAX = 500;

function NoteForm({ title, description, label, confirmLabel, destructive, onConfirm }: Omit<NoteDialogProps, "open" | "onOpenChange">) {
  const { t } = useI18n();
  const [note, setNote] = useState("");
  const inputId = useId();
  return (
    <>
      <AlertDialogTitle>{title}</AlertDialogTitle>
      <AlertDialogDescription>{description}</AlertDialogDescription>
      <div className="space-y-1.5">
        <label htmlFor={inputId} className="text-sm font-medium">
          {label}
        </label>
        <Input id={inputId} value={note} maxLength={NOTE_MAX} onChange={(e) => setNote(e.target.value)} className="h-11 sm:h-9" />
      </div>
      <div className="flex flex-col-reverse gap-2 sm:flex-row sm:justify-end">
        <AlertDialogCancel>{t("common.cancel")}</AlertDialogCancel>
        <AlertDialogAction destructive={destructive} onClick={() => onConfirm(note.trim())}>
          {confirmLabel}
        </AlertDialogAction>
      </div>
    </>
  );
}
