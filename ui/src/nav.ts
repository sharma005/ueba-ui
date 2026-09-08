import { useEffect, useRef, useState } from "react";
import { useLocation, useNavigationType } from "react-router-dom";

/**
 * Whether this tab has a screen to go back to inside the app. Mounted once, in
 * `Shell`: it has to outlive every route change to be a trail rather than a
 * guess. Keyed on `location.key`, not the path — the same path visited twice is
 * two entries.
 */
export function useCanGoBackTrail(): boolean {
  const { key } = useLocation();
  const type = useNavigationType();
  const trail = useRef<string[]>([]);
  const [canGoBack, setCanGoBack] = useState(false);

  useEffect(() => {
    const t = trail.current;
    // A replace does not deepen history — counting it would let Back walk off
    // the end of the trail and out of the app, on the tenant-switch redirect
    // and the legacy `<Navigate replace>` routes.
    if (type === "REPLACE" && t.length) t[t.length - 1] = key;
    else if (type === "POP") {
      const i = t.indexOf(key);
      // A known key means we stepped back onto it; an unknown one is a forward
      // press, which extends the trail like a push.
      if (i >= 0) t.length = i + 1;
      else t.push(key);
    } else t.push(key);
    setCanGoBack(t.length > 1);
  }, [key, type]);

  return canGoBack;
}
