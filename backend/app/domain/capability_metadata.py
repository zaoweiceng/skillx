"""One semantic metadata contract for generated and published workflows."""
from typing import Annotated
from pydantic import BaseModel, ConfigDict, Field, ValidationError

NonBlank = Annotated[str, Field(pattern=r'\S')]
NonEmptyTexts = Annotated[list[NonBlank], Field(min_length=1)]


class PublicationCapabilityMetadata(BaseModel):
    model_config = ConfigDict(extra='allow')
    summary: NonBlank
    useWhen: NonEmptyTexts
    doNotUseWhen: NonEmptyTexts
    examples: NonEmptyTexts = Field(description='用户可读的调用场景文字列表；不是 input/output 对象。可执行测试对象放在顶层 workflowExamples。')
    tags: NonEmptyTexts



class PublicationWorkflowMetadata(BaseModel):
    model_config = ConfigDict(extra='allow')
    name: NonBlank
    capability: PublicationCapabilityMetadata


def publication_metadata_errors(raw):
    try:
        PublicationWorkflowMetadata.model_validate(raw)
        return []
    except ValidationError as exc:
        return exc.errors(include_url=False, include_input=False)


def validate_publication_metadata(raw):
    errors = publication_metadata_errors(raw)
    if errors:
        raise ValueError('发布元数据契约不一致：' + '; '.join(
            'metadata.' + '.'.join(map(str, error['loc'])) + ': ' + error['msg'] for error in errors))
