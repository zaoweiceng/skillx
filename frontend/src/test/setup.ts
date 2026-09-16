import '@testing-library/jest-dom/vitest'

// Browser layout APIs used by Ant Design; geometry is verified by Playwright.
class ResizeObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}
globalThis.ResizeObserver = ResizeObserverStub
Object.defineProperty(window, 'matchMedia', {
  writable: true, value: (query: string) => ({ matches: false, media: query, onchange: null,
    addListener() {}, removeListener() {}, addEventListener() {}, removeEventListener() {}, dispatchEvent: () => false }),
})
