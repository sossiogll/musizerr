import type { ReactNode } from "react";
import { ArrowUpDown, ChevronDown, Search, X } from "lucide-react";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { useI18n } from "@/i18n";
import {
  DropdownMenu,
  DropdownMenuTrigger,
  DropdownMenuContent,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuSeparator,
} from "@/components/ui/dropdown-menu";

export interface SortOption<K extends string = string> {
  value: K;
  label: string;
}

export interface FilterChip {
  key: string;
  label: string;
  options: { value: string; label: string }[];
  value: string;
  onChange: (value: string) => void;
}

interface ListToolbarProps<K extends string = string> {
  filter?: {
    value: string;
    onChange: (value: string) => void;
    placeholder?: string;
  };

  chips?: FilterChip[];

  sort?: {
    value: K;
    onChange: (value: K) => void;
    options: SortOption<K>[];
  };

  extra?: ReactNode;
}

export function ListToolbar<K extends string = string>({
  filter,
  chips,
  sort,
  extra,
}: ListToolbarProps<K>) {
  const { t } = useI18n();
  return (
    <div className="flex flex-col gap-2 sm:flex-row sm:flex-wrap sm:items-center">
      {filter && (
        <div className="relative w-full sm:w-auto sm:flex-1 sm:max-w-xs">
          <Search aria-hidden className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
          <Input
            type="search"
            aria-label={filter.placeholder ?? t("common.filterPlaceholder")}
            placeholder={filter.placeholder ?? t("common.filterPlaceholder")}
            value={filter.value}
            onChange={(e) => filter.onChange(e.target.value)}
            className="h-11 pl-9 pr-10 text-sm sm:h-9 [&::-webkit-search-cancel-button]:hidden"
          />
          {filter.value && (
            <button
              type="button"
              aria-label={t("common.clearFilter")}
              onClick={() => filter.onChange("")}
              className="absolute right-0 top-1/2 grid size-11 -translate-y-1/2 place-items-center text-muted-foreground hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring rounded-md sm:size-9"
            >
              <X aria-hidden className="size-4" />
            </button>
          )}
        </div>
      )}

      {/* Su mobile chip+extra+sort scorrono orizzontalmente (niente wrap
          disordinato); su sm+ il wrapper si dissolve (contents) e tornano
          elementi inline della riga con flex-wrap + ml-auto sul sort. */}
      {(chips?.length || extra || sort) && (
        <div className="flex items-center gap-2 overflow-x-auto pb-1 -mb-1 sm:contents [scrollbar-width:none] [&::-webkit-scrollbar]:hidden">
          {chips?.map((chip) => (
            <ChipDropdown key={chip.key} chip={chip} />
          ))}

          {extra}

          {sort && (
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <Button variant="outline" size="sm" className="ml-auto h-11 shrink-0 gap-1.5 sm:h-9" aria-label={t("common.sortLabel")}>
                  <ArrowUpDown aria-hidden className="size-3.5" />
                  <span className="hidden sm:inline">
                    {sort.options.find((o) => o.value === sort.value)?.label ?? t("common.sortLabel")}
                  </span>
                </Button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="end" className="w-44">
                <DropdownMenuRadioGroup
                  value={sort.value}
                  onValueChange={(v) => sort.onChange(v as K)}
                >
                  {sort.options.map((opt) => (
                    <DropdownMenuRadioItem key={opt.value} value={opt.value}>
                      {opt.label}
                    </DropdownMenuRadioItem>
                  ))}
                </DropdownMenuRadioGroup>
              </DropdownMenuContent>
            </DropdownMenu>
          )}
        </div>
      )}
    </div>
  );
}

function ChipDropdown({ chip }: { chip: FilterChip }) {
  const selected = chip.options.find((o) => o.value === chip.value);
  const displayLabel = selected ? selected.label : chip.label;

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button
          variant={chip.value ? "secondary" : "outline"}
          size="sm"
          className="h-11 shrink-0 gap-1 sm:h-9"
        >
          {displayLabel}
          <ChevronDown aria-hidden className="size-3" />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="start" className="w-44">
        <DropdownMenuRadioGroup
          value={chip.value}
          onValueChange={chip.onChange}
        >
          <DropdownMenuRadioItem value="">
            {chip.label}
          </DropdownMenuRadioItem>
          <DropdownMenuSeparator />
          {chip.options.map((opt) => (
            <DropdownMenuRadioItem key={opt.value} value={opt.value}>
              {opt.label}
            </DropdownMenuRadioItem>
          ))}
        </DropdownMenuRadioGroup>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
