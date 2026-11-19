export type Lab = [number, number, number]
export function hexToRgb(hex: string): [number, number, number]
export function rgbToHex(rgb: number[]): string
export function linearToOklab(rgb: number[]): Lab
export function oklabToLinear(lab: number[]): Lab
export function hexToOklab(hex: string): Lab
export function oklabToHex(lab: number[]): string
export function ramp(stops: string[], n: number): string[]
export function luminance(hex: string): number
export function contrast(a: string, b: string): number
export const CVD_KINDS: string[]
export function simulate(hex: string, kind: string): Lab
export function minSeparation(colors: string[], kind: string): { min: number; pair: [number, number] | null }
