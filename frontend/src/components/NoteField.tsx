import { Button, TextInput } from "@mantine/core";
import { useState } from "react";

/**
 * The optional decision note, hidden behind "Add note" until used (UI-REFRESH-11).
 * The value lives in the caller, so it is kept until the item is decided.
 */
export function NoteField({
  value,
  onChange,
  disabled = false,
}: {
  value: string;
  onChange: (value: string) => void;
  disabled?: boolean;
}) {
  const [open, setOpen] = useState(value !== "");
  if (!open) {
    return (
      <Button
        variant="subtle"
        size="compact-xs"
        disabled={disabled}
        onClick={() => {
          setOpen(true);
        }}
      >
        Add note
      </Button>
    );
  }
  return (
    <TextInput
      size="xs"
      label="Note (optional)"
      value={value}
      disabled={disabled}
      autoFocus
      onChange={(e) => {
        onChange(e.currentTarget.value);
      }}
      w={260}
    />
  );
}
