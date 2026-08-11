// View state in the URL.
//
// This was a hand-rolled `URLSearchParams` synced to `window.location`, written
// before the router landed and deliberately given `useSearchParams`' exact
// read/write shape so that swapping the implementation would need no change in
// any caller. That swap is this file: the router is here, and the hook is now a
// thin alias over `useSearchParams`.
//
// It stays a named export rather than being deleted at the call sites, because
// there is exactly one query-param hook in the app and this is its name. The
// information architecture it serves is unchanged: path segments identify
// objects (`/p/:project/r/:runId/grouping`), query params identify view state
// (`?group=`, `?stage=`, `?edge=`, `?seq=`).
//
// Push rather than replace, which is `useSearchParams`' default: scrubbing to
// the merge stage and hitting back should return to the stage before it. That
// is what makes the stepper feel like navigation rather than a widget.

import { useSearchParams } from "react-router-dom";

export type SetQueryParams = (next: URLSearchParams) => void;

export function useQueryParams(): [URLSearchParams, SetQueryParams] {
  const [params, setParams] = useSearchParams();
  return [params, setParams as SetQueryParams];
}
