// View state in the URL, without taking a router dependency.
//
// The agreed information architecture is `react-router-dom` v6 with
// `createBrowserRouter`, path segments identifying objects and query params
// identifying view state (`?tab=`, `?group=`, `?stage=`, `?edge=`, `?seq=`).
// That router — and the tab shell around it — belongs to the shell group, and
// building a second one here would be exactly the parallel copy that has to be
// unpicked later.
//
// So this is the smallest thing that makes the query-param half real today: a
// `URLSearchParams` synced to `window.location`, with the same read/write shape
// `useSearchParams` has. When the router lands, the import swaps to
// `react-router-dom` and every caller keeps compiling.
//
// `pushState` rather than `replaceState`: scrubbing to the merge stage and
// hitting back should return to the stage before it. That is what makes the
// stepper feel like navigation rather than a widget.

import { useCallback, useEffect, useState } from "react";

export type SetQueryParams = (next: URLSearchParams) => void;

export function useQueryParams(): [URLSearchParams, SetQueryParams] {
  const [search, setSearch] = useState(() => window.location.search);

  useEffect(() => {
    const onPop = () => setSearch(window.location.search);
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);

  const setParams = useCallback<SetQueryParams>((next) => {
    const query = next.toString();
    const url = `${window.location.pathname}${query ? `?${query}` : ""}${window.location.hash}`;
    window.history.pushState(null, "", url);
    setSearch(query ? `?${query}` : "");
  }, []);

  return [new URLSearchParams(search), setParams];
}
