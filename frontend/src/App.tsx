import { lazy, Suspense } from 'react'
import { Spin } from 'antd'
import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom'

import AppShell from '@/components/AppShell'
const CapabilityDetailPage = lazy(() => import('@/pages/CapabilityDetailPage'))
const CapabilitiesPage = lazy(() => import('@/pages/CapabilitiesPage'))
import ChatPage from '@/pages/ChatPage'
const ComponentDetailPage = lazy(() => import('@/pages/ComponentDetailPage'))
const RunDetailPage = lazy(() => import('@/pages/RunDetailPage'))
const SettingsPage = lazy(() => import('@/pages/SettingsPage'))
const SkillsPage = lazy(() => import('@/pages/SkillsPage'))
const WorkflowEditorPage = lazy(() => import('@/pages/WorkflowEditorPage'))
const WorkflowExecutePage = lazy(() => import('@/pages/WorkflowExecutePage'))
const WorkflowsPage = lazy(() => import('@/pages/WorkflowsPage'))

export default function App() {
  return (
    <BrowserRouter>
      <Suspense fallback={<div className="route-loading"><Spin size="large" /></div>}>
      <Routes>
        <Route element={<AppShell />}>
          <Route path="/" element={<Navigate to="/chat" replace />} />
          <Route path="/chat" element={<ChatPage />} />
          <Route path="/chat/:id" element={<ChatPage />} />
          <Route path="/components" element={<Navigate to="/workflows" replace />} />
          <Route path="/components/:id" element={<ComponentDetailPage />} />
          <Route path="/runs/:id" element={<RunDetailPage />} />
          <Route path="/workflows" element={<WorkflowsPage />} />
          <Route path="/workflows/published/:id" element={<ComponentDetailPage />} />
          <Route path="/workflows/:id/run" element={<WorkflowExecutePage />} />
          <Route path="/workflows/:id/edit" element={<WorkflowEditorPage />} />
          <Route path="/capabilities/:id" element={<CapabilityDetailPage />} />
          <Route path="/capabilities" element={<CapabilitiesPage />} />
          <Route path="/settings" element={<SettingsPage />} />
          <Route path="/skills" element={<SkillsPage />} />
          <Route path="*" element={<Navigate to="/chat" replace />} />
        </Route>
      </Routes>
      </Suspense>
    </BrowserRouter>
  )
}
