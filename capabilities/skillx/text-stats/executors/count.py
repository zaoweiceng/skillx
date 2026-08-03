from app.runtime.skillx import SkillXContext, SkillXRequest, SkillXResult


async def execute(
    request: SkillXRequest,
    context: SkillXContext,
) -> SkillXResult:
    context.check_cancelled()
    text = request.input["text"]
    return SkillXResult(
        output={
            "characters": len(text),
            "nonWhitespaceCharacters": sum(not character.isspace() for character in text),
            "words": len(text.split()),
        }
    )
