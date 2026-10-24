import { Badge } from "@/components/ui/badge";
import { useI18n } from "@/i18n";

/** The roles a provider fills, in the words the Providers page uses for them. */
export function RoleBadges({ roles }: { roles: string[] }) {
  const { tx } = useI18n();
  return (
    <>
      {roles.map((role) => (
        <Badge key={role} variant="secondary">
          {tx(`providersTab.${role}`, role)}
        </Badge>
      ))}
    </>
  );
}
