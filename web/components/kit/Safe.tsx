"use client";

import { Component, type ReactNode } from "react";

/**
 * Error boundary for one section of a page: if a payload field has an unexpected shape, only this card
 * shows a short note and the rest of the page keeps working.
 */
export class Safe extends Component<{ children: ReactNode; label?: string }, { error: Error | null }> {
  state: { error: Error | null } = { error: null };

  static getDerivedStateFromError(error: Error) {
    return { error };
  }

  componentDidCatch(error: Error) {
    console.error(`[console] ${this.props.label ?? "section"} failed to render`, error);
  }

  render() {
    if (this.state.error) {
      return (
        <div className="card" role="status">
          <div className="semi">{this.props.label ?? "This part"} could not be shown</div>
          <div className="muted xs" style={{ marginTop: 6 }}>The data has a shape this screen does not expect. The rest of the page is fine; the Raw JSON button shows everything.</div>
        </div>
      );
    }
    return this.props.children;
  }
}
