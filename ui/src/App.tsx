// The app is its route table.
//
// What used to be here — a `?tab=` strip, one `useRunStream`, and every panel
// composed in a single conditional — was a deliberate placeholder for this.
// The tab strip is gone; `RunLayout` owns the shell, `routes.tsx` owns the
// table, and no component's props changed to get here, because the query-param
// hook the placeholder used was written with `useSearchParams`' shape.
//
// `createBrowserRouter` rather than `HashRouter`: the backend's SPA catch-all
// is landed (`observatory/app.py:_mount_spa`), so a refresh on
// `/p/proj/r/run/grouping?group=g2` reaches `index.html` and renders. Hash
// routing was only ever the documented fallback if a server catch-all were
// refused.

import { RouterProvider, createBrowserRouter } from "react-router-dom";

import { routes } from "./routes";

const router = createBrowserRouter(routes);

function App() {
  return <RouterProvider router={router} />;
}

export default App;
