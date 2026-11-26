/// <reference types="vite/client" />

interface GridForgeBrandConfig {
  name?: string;
  subtitle?: string;
  tagline?: string;
}

interface Window {
  __GF_BRAND__?: GridForgeBrandConfig;
}
