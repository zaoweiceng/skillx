import { clsx, type ClassValue } from "clsx"
import { twMerge } from "tailwind-merge"

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs))
}

export function rjsfSchema(schema: Record<string, unknown>): Record<string, unknown> {
  const formSchema = { ...schema }
  delete formSchema.$schema
  return formSchema
}
