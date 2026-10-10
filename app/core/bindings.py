"""Generate TypeScript interfaces from the step language."""
from __future__ import annotations

TEMPLATE = '''
export interface Step {
  action: "click" | "fill" | "press" | "capture" | "scroll" | "wait" | "goto" | "download" | "ensure_login";
  selector?: string;
  value?: string;
  key?: string;
  url?: string;
  timeout?: number;
  optional?: boolean;
}
export interface Journey {
  name: string;
  url: string;
  steps: Step[];
}
'''

def check_syntax(text: str) -> bool:
    return 'action' in text and 'Step' in text

def generate_ts() -> str:
    return TEMPLATE
