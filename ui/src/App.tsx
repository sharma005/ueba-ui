import { HashRouter, Navigate, Route, Routes } from "react-router-dom";
import { ErrorBoundary } from "./components/ErrorBoundary";
import { Shell } from "./components/Shell";
import Alerts from "./pages/Alerts";
import Anomalies from "./pages/Anomalies";
import Dashboard from "./pages/Dashboard";
import UserDetail from "./pages/UserDetail";
import Users from "./pages/Users";

export default function App() {
  return (
    <ErrorBoundary>
      <HashRouter>
        <Routes>
          <Route element={<Shell />}>
            <Route index element={<Dashboard />} />
            <Route path="/users" element={<Users />} />
            <Route path="/users/:id" element={<UserDetail />} />
            <Route path="/anomalies" element={<Anomalies />} />
            <Route path="/alerts" element={<Alerts />} />
            <Route path="/chains" element={<Navigate to="/" replace />} />
            <Route path="/correlations" element={<Navigate to="/" replace />} />
            <Route path="/entities" element={<Navigate to="/users" replace />} />
            <Route path="/entities/:id" element={<Navigate to="/users" replace />} />
            <Route path="/peers" element={<Navigate to="/users" replace />} />
            <Route path="/graph" element={<Navigate to="/users" replace />} />
            <Route path="*" element={<Navigate to="/" replace />} />
          </Route>
        </Routes>
      </HashRouter>
    </ErrorBoundary>
  );
}
