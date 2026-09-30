import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, expect, it, vi } from 'vitest'
import WorkflowsPage from './WorkflowsPage'

vi.mock('@/api/client', () => ({api: {
  listWorkflows: async () => [
    {id: 'published', name: '邮件整理', definition: {metadata: {name: 'internal-slug', capability: {summary: '规范邮件地址并去重'}}}},
    {id: 'draft', name: '新流程', definition: {metadata: {description: '整理草稿中的数据'}}},
  ],
  listComponents: async () => [{id: 'component', workflowId: 'published', name: '邮件整理', description: '规范邮件地址并去重', executionClass: 'zero_llm'}],
}}))
vi.mock('@/api/http', () => ({request: async () => [{capabilityId: 'published'}]}))
afterEach(cleanup)

it('合并发布入口与草稿，展示简介并保留运行和编辑路径', async () => {
  render(<MemoryRouter><WorkflowsPage /></MemoryRouter>)
  expect(await screen.findByRole('heading', {name: '邮件整理'})).toBeInTheDocument()
  expect(screen.getAllByRole('heading', {name: '邮件整理'})).toHaveLength(1)
  expect(screen.getByRole('link', {name: /邮件整理/})).toHaveAttribute('href', '/workflows/published/component')
  expect(screen.getByRole('link', {name: /新流程/})).toHaveAttribute('href', '/workflows/draft/edit')
  expect(screen.getAllByRole('link', {name: /编辑/})).toHaveLength(2)
  expect(screen.queryByText('internal-slug')).not.toBeInTheDocument()
  fireEvent.change(screen.getByPlaceholderText('搜索工作流名称或用途'), {target: {value: '去重'}})
  expect(screen.queryByRole('heading', {name: '新流程'})).not.toBeInTheDocument()
  fireEvent.change(screen.getByPlaceholderText('搜索工作流名称或用途'), {target: {value: ''}})
  fireEvent.click(screen.getByRole('button', {name: /草\s*稿/}))
  expect(screen.getByRole('heading', {name: '新流程'})).toBeInTheDocument()
  expect(screen.queryByRole('heading', {name: '邮件整理'})).not.toBeInTheDocument()
})
