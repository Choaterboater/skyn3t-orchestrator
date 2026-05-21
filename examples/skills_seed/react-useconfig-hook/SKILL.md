---
name: react-useconfig-hook
description: Provide a useConfig hook for runtime frontend configuration.
tags: [react, config, hook, scaffold-pattern]
triggers: [react, vite, nextjs, frontend]
---

# React useConfig Hook

When scaffolding a React frontend that talks to a backend, always include a `useConfig` hook that reads runtime settings from `localStorage` (with a sensible default). This avoids rebuilding the bundle every time the API URL changes.

## Why it matters

Hardcoded `fetch('/api/...')` URLs break when the app is deployed behind a reverse proxy or when the backend port changes. A config hook makes the frontend environment-aware without env-var rebuilds.

## Code

```javascript
// src/hooks/useConfig.js
import { useState, useEffect } from 'react';

const DEFAULTS = {
  API_BASE_URL: 'http://localhost:3100',
  REFRESH_INTERVAL_MS: 30000,
};

export function useConfig() {
  const [config, setConfig] = useState(() => {
    try {
      const raw = localStorage.getItem('appConfig');
      return raw ? { ...DEFAULTS, ...JSON.parse(raw) } : DEFAULTS;
    } catch {
      return DEFAULTS;
    }
  });

  useEffect(() => {
    localStorage.setItem('appConfig', JSON.stringify(config));
  }, [config]);

  return { config, setConfig };
}
```

## Usage

```jsx
import { useConfig } from '../hooks/useConfig';

function ServiceList() {
  const { config } = useConfig();
  useEffect(() => {
    fetch(`${config.API_BASE_URL}/api/services`)
      .then(r => r.json())
      .then(setServices);
  }, [config.API_BASE_URL]);
}
```
