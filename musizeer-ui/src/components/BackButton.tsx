import { useRouter } from "@tanstack/react-router";
import { ArrowLeft } from "lucide-react";
import { Button } from "@/components/ui/button";
import { useI18n } from "@/i18n";

export function BackButton() {
  const router = useRouter();
  const { t } = useI18n();
  return (
    <Button
      variant="ghost"
      size="sm"
      onClick={() => router.history.back()}
      className="gap-1.5 text-muted-foreground hover:text-foreground max-sm:h-11"
    >
      <ArrowLeft aria-hidden className="size-4" />
      {t("common.back")}
    </Button>
  );
}
