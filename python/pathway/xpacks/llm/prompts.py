import functools
import re
from abc import ABC, abstractmethod
from typing import Any, Callable

from pydantic import BaseModel, field_validator

import pathway as pw


class BasePromptTemplate(BaseModel, ABC):
    class Config:
        arbitrary_types_allowed = True

    @abstractmethod
    def as_udf(self, **kwargs: Any) -> pw.UDF: ...


class FunctionPromptTemplate(BasePromptTemplate):
    """
    Utility class to create prompt templates from callables or UDF.

    ``as_udf`` may take kwargs to partially pre-fill the prompt template.
    """

    function_template: Callable[[str, str], str] | pw.UDF

    def as_udf(self, **kwargs: Any) -> pw.UDF:
        if isinstance(self.function_template, pw.UDF):
            return self.function_template
        return pw.udf(functools.partial(self.function_template, **kwargs))


class StringPromptTemplate(BasePromptTemplate):
    """
    Utility class to create prompt templates that can be applied to tables.

    >>> import pandas as pd
    >>> import pathway as pw
    >>> prompt_template = "Answer the following question. Context: {context}. Question: {query}"
    >>> t = pw.debug.table_from_pandas(pd.DataFrame([{"context": "Here are some facts...",
    ...     "query": "How much do penguins weigh in average?"}]))
    >>> template = StringPromptTemplate(template=prompt_template)
    >>> template_udf = template.as_udf()
    >>> t = t.select(prompt=template_udf(context=pw.this.context, query=pw.this.query))
    """

    template: str

    def format(self, **kwargs: Any) -> str:
        return self.template.format(**kwargs)

    def as_udf(self, **kwargs: Any) -> pw.UDF:
        @pw.udf
        def udf_formatter(context: str, query: str) -> str:
            return self.format(query=query, context=context, **kwargs)

        return udf_formatter


class RAGPromptTemplate(StringPromptTemplate):
    @field_validator("template")
    @classmethod
    def is_valid_rag_template(cls, template: str) -> str:
        if "{context}" not in template or "{query}" not in template:
            raise ValueError(
                "Template must contain `{context}` and `{query}` placeholders."
            )

        try:
            template.format(context=" ", query=" ")
        except KeyError:
            raise ValueError(
                "RAG prompt template expects `context` and `query` placeholders only."
            )
        return template


class RAGFunctionPromptTemplate(FunctionPromptTemplate):

    @field_validator("function_template")
    @classmethod
    def is_valid_rag_template(cls, template: Callable | pw.UDF) -> pw.UDF:
        if isinstance(template, pw.UDF):
            fn: Callable = template.__wrapped__
        else:
            fn = template
            template = pw.udf(template)

        try:
            fn(query=" ", context=" ")
        except TypeError as e:
            raise ValueError(
                "RAG prompt template expects `context` and `query` placeholders only.\n"
                + str(e)
            )
        return template


@pw.udf
def prompt_short_qa(context: str, query: str, additional_rules: str = "") -> str:
    """
    Generate a RAG prompt with given context.

    Specifically for getting short and concise answers.
    Given a question, and list of context documents, generates prompt to be sent to the LLM.
    Suggests specific formatting for yes/no questions and dates.

    Args:
        context: Information sources or the documents to be passed to the LLM as context.
        query: Question or prompt to be answered.
        additional_rules: Optional parameter for rest of the string args that may include
            additional instructions or information.

    Returns:
        Prompt containing question and the relevant docs.
    """

    prompt = (
        "Please provide an answer based solely on the provided sources. "
        "Keep your answer concise and accurate. Make sure that it starts with an expression in standardized format. "
        "Only respond without any explanation, for example questions asking for date should be answered in strictly date format: `05 January 2011`. "  # noqa: E501
        "Yes or No questions should be responded with simple `Yes` or `No` and so on. "
        "If question cannot be inferred from documents SAY `No information found`. "
    )

    prompt += additional_rules + " "

    prompt += (
        "Now it's your turn. Below are several sources of information:"
        "\n------\n"
        f"{context}"
        "\n------\n"
        f"Query: {query}\n"
        "Answer:"
    )
    return prompt


@pw.udf
def prompt_qa(
    context: str,
    query: str,
    information_not_found_response="No information found.",
    additional_rules: str = "",
) -> str:
    """
    Generate RAG prompt with given context.

    Given a question and list of context documents, generates prompt to be sent to the LLM.

    Args:
        context: Information sources or the documents to be passed to the LLM as context.
        query: Question or prompt to be answered.
        information_not_found_response: Response LLM should generate in case answer cannot
            be inferred from the given documents.
        additional_rules: Optional parameter for rest of the string args that may include
            additional instructions or information.

    Returns:
        Prompt containing question and relevant docs.

    >>> import pandas as pd
    >>> import pathway as pw
    >>> from pathway.xpacks.llm import prompts
    >>> t = pw.debug.table_from_pandas(pd.DataFrame([{"context": "Here are some facts...",
    ...     "query": "How much do penguins weigh in average?"}]))
    >>> r = t.select(prompt=prompts.prompt_qa(pw.this.context, pw.this.query))
    """  # noqa: E501

    prompt = (
        "Please provide an answer based solely on the provided sources. "
        "Keep your answer concise and accurate. "
    )

    prompt += additional_rules + " "

    prompt += (
        f"If question cannot be inferred from documents SAY `{information_not_found_response}`. "
        "Now it's your turn. Below are several sources of information:"
        "\n------\n"
        f"{context}"
        "\n------\n"
        f"Query: {query}\n"
        "Answer:"
    )
    return prompt


# prompt for `answer_with_geometric_rag_strategy`, it is the same as in the research project
# docs` argument will be deprecated in favor of `context: str` argument
# this will require the use of `BaseContextProcessor`
@pw.udf
def prompt_qa_geometric_rag(
    query: str,
    docs: list[pw.Json] | list[str],
    information_not_found_response="No information found.",
    additional_rules: str = "",
    strict_prompt: bool = False,  # instruct LLM to return json for local models, improves performance
):
    context_pieces = []

    for i, doc in enumerate(docs, 1):
        if isinstance(doc, str):
            context_pieces.append(f"Source {i}: {doc}")
        else:
            context_pieces.append(f"Source {i}: {doc['text']}")  # type: ignore
    context_str = "\n".join(context_pieces)

    if strict_prompt:
        prompt = f"""
        Use the below articles to answer the subsequent question. If the answer cannot be found in the articles, write "{information_not_found_response}" Do not explain.
        ONLY RESPOND IN PARSABLE JSON WITH THE ONLY KEY `answer`.
        When referencing information from a source, cite the appropriate source(s) using their corresponding numbers. Every answer should include at least one source citation.
        Only cite a source when you are explicitly referencing it.
        For example:
        Given following sources and query
        Example 1: "Source 1: The sky is red in the evening and blue in the morning.\nSource 2: Water is wet when the sky is red.
        Query: When is water wet?
        Response: {{"answer": "When the sky is red [2], which occurs in the evening [1]."}}
        Example 2: "Source 1: LLM stands for Large language models.
        Query: Who is the current pope?
        Response: {{"answer": "{information_not_found_response}"}}
        """  # noqa
    else:
        prompt = f"""
        Use the below articles to answer the subsequent question. If the answer cannot be found in the articles, write "{information_not_found_response}" Do not answer in full sentences.
        When referencing information from a source, cite the appropriate source(s) using their corresponding numbers. Every answer should include at least one source citation.
        Only cite a source when you are explicitly referencing it. For example:
        "Source 1:
        The sky is red in the evening and blue in the morning.
        Source 2:
        Water is wet when the sky is red.\n
        Query: When is water wet?
        Answer: When the sky is red [2], which occurs in the evening [1]."
        """  # noqa

    prompt += additional_rules + " "

    if strict_prompt:  # further instruction is needed for smaller models
        prompt += (
            "\n------\n"
            f"{context_str}"
            f"Query: {query}\n"
            "ONLY RESPOND IN PARSABLE JSON WITH THE ONLY KEY `answer` containing your response. "
        )

        response_str = "Response"
    else:
        prompt += (
            "Now it's your turn. "
            "\n------\n"
            f"{context_str}"
            "\n------\n"
            f"Query: {query}\n"
        )

        response_str = "Answer"

    prompt += f"{response_str}:"
    return prompt


# citations


@pw.udf
def prompt_citing_qa(context: str, query: str, additional_rules: str = "") -> str:
    """
    Generate RAG prompt that instructs the LLM to give citations with the answer.

    Given a question and list of context documents, generates prompt to be sent to the LLM.

    Args:
        context: Information sources or the documents to be passed to the LLM as context.
        query: Question or prompt to be answered.
        additional_rules: Optional parameter for rest of the string args that may include
            additional instructions or information.

    Returns:
        Prompt containing question and the relevant docs.

    >>> import pandas as pd
    >>> import pathway as pw
    >>> from pathway.xpacks.llm import prompts
    >>> t = pw.debug.table_from_pandas(pd.DataFrame([{"context": "Here are some facts...",
    ...     "query": "How much do penguins weigh in average?"}]))
    >>> r = t.select(prompt=prompts.prompt_citing_qa(pw.this.context, pw.this.query))
    """  # noqa: E501

    prompt = (
        "Please provide an answer based solely on the provided sources. "
        "When referencing information from a source, "
        "cite the appropriate source(s) using their corresponding numbers. "
        "Every answer should include at least one source citation. "
        "Only cite a source when you are explicitly referencing it. "
        "If exists, mention specific article/section header you use at the beginning of answer, such as '4.a Client has rights to...'. "  # noqa: E501
        "Article headers may or may not be in docs, dont mention it if there is none. "
        "If question cannot be inferred from documents SAY `No information found`. "
    )

    prompt += additional_rules + " "

    prompt += (
        "Now it's your turn. Below are several numbered sources of information:"
        "\n------\n"
        f"{context}"
        "\n------\n"
        f"Query: {query}\n"
        "Answer:"
    )
    return prompt


@pw.udf
def parse_cited_response(response_text, docs):
    cited_docs = [
        int(cite[1:-1]) - 1
        for cite in set(re.findall("\[\d+\]", response_text))  # noqa: W605
    ]
    start_index = response_text.find("*") + 1
    end_index = response_text.find("*", start_index)

    citations = [docs[i] for i in cited_docs if i in cited_docs]
    cleaned_citations = []

    if (
        start_index != -1 and end_index != -1
    ):  # doing this for the GIF, we need a better way to do this, TODO: redo
        cited = response_text[start_index:end_index]
        response_text = response_text[end_index:].strip()
        cited = (
            cited.replace(" ", "")
            .replace(",,", ",")
            .replace(",", ",\n")
            .replace(" ", "\n")
        )

        text_body = citations[0]["text"]
        new_text = f"<b>{cited}</b>\n\n".replace("\n\n\n", "\n") + text_body

        citations[0]["text"] = new_text

        cleaned_citations.append(citations[0])

    if len(citations) > 1:
        for doc in citations[1:]:
            text_body = doc["text"]  # TODO: unformat and clean the text
            doc["text"] = text_body
            cleaned_citations.append(doc)

    return response_text, cleaned_citations


# summarization


@pw.udf
def prompt_summarize(text_list: list[str]) -> str:
    """
    Generate a summarization prompt with the list of texts.

    Args:
        text_list: List of text documents.

    Returns:
        Summarized text.
    """
    text = "\n".join(text_list)
    prompt = f"""Given a list of documents, summarize them in few sentences \
        while preserving important points and entities.
    Documents: {text}
    Summary:"""

    return prompt


# query re-writing


@pw.udf
def prompt_query_rewrite_hyde(query: str) -> str:
    """
    Generate prompt for query rewriting using the HyDE technique.

    Args:
        query: Original search query or user prompt.

    Returns:
        Transformed query.
    """

    prompt = f"""Write 4 responses to answer the given question with hypothetical data.
    Try to include as many key details as possible.
    Question: `{query}`.
    Responses:"""
    return prompt


@pw.udf
def prompt_query_rewrite(query: str, *additional_args: str) -> str:
    """
    Generate prompt for query rewriting.

    Prompt function to generate and augment index search queries using important names,
    entities and information from the given input. Generates three transformed queries
    concatenated with comma to improve the search performance.

    Args:
        query: Original search query or user prompt.
        additional_args: Additional information that may help LLM in generating the query.

    Returns:
        Transformed query.
    """
    prompt = f"""Given a question that will be used to retrieve similar documents for RAG application.
    Rewrite question to be better usable in retrieval search.
    Use important entities, words that may be related to query and other entity names.
    Your response should be three queries based on the question provided, separated by comma.
    Question: `{query}`
    """

    if additional_args:
        prompt += """If any of the provided sections are related to question, write section name in the query as well.
        Here is additional info that you can include in search: """
    for arg in additional_args:
        prompt += f" `{arg}`\n"

    prompt += "Rewritten query:"
    return prompt


# default system prompts

DEFAULT_JSON_TABLE_PARSE_PROMPT = """Explain the given table in JSON format in detail.
Do not skip over details or units/metrics.
Make sure column and row names are understandable.
If it is not a table, return 'No table.'."""

DEFAULT_MD_TABLE_PARSE_PROMPT = """Explain the given table in markdown format in detail.
Do not skip over details or units/metrics.
Make sure column and row names are understandable.
If it is not a table, return 'No table.'."""

DEFAULT_IMAGE_PARSE_PROMPT = """Explain the given image in detail.
If there is text, make sure to spell out all the text.
If info is formatted as table, your output should be also formatted."""
