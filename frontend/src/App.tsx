import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom'

import AppShell from '@/components/AppShell'
import CapabilitiesPage from '@/pages/CapabilitiesPage'
import ChatPage from '@/pages/ChatPage'
import ComponentDetailPage from '@/pages/ComponentDetailPage'
import ComponentsPage from '@/pages/ComponentsPage'
import RunDetailPage from '@/pages/RunDetailPage'
import SettingsPage from '@/pages/SettingsPage'
import WorkflowEditorPage from '@/pages/WorkflowEditorPage'
import WorkflowsPage from '@/pages/WorkflowsPage'

export default function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route element={<AppShell />}>
          <Route path="/" element={<Navigate to="/chat" replace />} />
          <Route path="/chat" element={<ChatPage />} />
          <Route path="/chat/:id" element={<ChatPage />} />
          <Route path="/components" element={<ComponentsPage />} />
          <Route path="/components/:id" element={<ComponentDetailPage />} />
          <Route path="/runs/:id" element={<RunDetailPage />} />
          <Route path="/workflows" element={<WorkflowsPage />} />
          <Route path="/workflows/:id/edit" element={<WorkflowEditorPage />} />
          <Route path="/capabilities" element={<CapabilitiesPage />} />
          <Route path="/settings" element={<SettingsPage />} />
          <Route path="*" element={<Navigate to="/chat" replace />} />
        </Route>
      </Routes>
    </BrowserRouter>
  )
}
