import { Image } from 'antd'
import { X } from 'lucide-react'
import { API_BASE } from '@/api/http'
import type { ChatImageReference } from '@/api/client'
import styles from './chat.module.css'

export function ChatImageAttachments({ images, onRemove, disabled }: {
  images: ChatImageReference[]
  onRemove?: (index: number) => void
  disabled?: boolean
}) {
  if (!images.length) return null
  return <div className={styles.imageAttachments} aria-label="图片附件">
    <Image.PreviewGroup>{images.map((image, index) => <div className={styles.imageAttachment} key={`${image.url}-${index}`} title={image.name}>
      <Image src={image.url.startsWith('data:') ? image.url : `${API_BASE}${image.url}`} alt={image.name} width={76} height={76} />
      {onRemove && <button type="button" className={styles.removeImage} aria-label={`删除图片 ${image.name}`} disabled={disabled}
        onClick={() => onRemove(index)}><X size={12} /></button>}
    </div>)}</Image.PreviewGroup>
  </div>
}
