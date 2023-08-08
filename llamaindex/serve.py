from transformers import AutoModelForCausalLM
from peft import PeftModel, PeftConfig
import torch
import os
from transformers import AutoTokenizer
from transformers import LlamaForCausalLM, LlamaTokenizer
from peft import LoraConfig, TaskType, get_peft_model
import bentoml

import time
import typing as t
from typing import TYPE_CHECKING


import bentoml
from bentoml.io import JSON
from bentoml.io import Text

import torch
from typing import Optional, List, Mapping, Any

from llama_index import ServiceContext, SimpleDirectoryReader, LangchainEmbedding, ListIndex
from llama_index import ServiceContext, SimpleDirectoryReader, VectorStoreIndex
from llama_index.llms import CustomLLM, CompletionResponse, CompletionResponseGen, LLMMetadata, llm_callback

# custom LLM class for llamaindex
class Llama2Model(CustomLLM):
    def __init__(self):
        super().__init__()
        peft_model_id = "/ckpt/"
        max_memory = {0: "80GIB", 1: "80GIB", "cpu": "30GB"}
        config = PeftConfig.from_pretrained(peft_model_id)
        model = AutoModelForCausalLM.from_pretrained(config.base_model_name_or_path, device_map="auto", torch_dtype=torch.float16)
        model = PeftModel.from_pretrained(model, peft_model_id, device_map="auto", max_memory=max_memory)
        model.eval()
        self.model = model
        tokenizer = AutoTokenizer.from_pretrained(config.base_model_name_or_path, legacy=False)
        tokenizer.pad_token = tokenizer.unk_token
        self.tokenizer = tokenizer

    @property
    def metadata(self) -> LLMMetadata:
        """Get LLM metadata."""
        return LLMMetadata(name="custom-llama2")

    @llm_callback()
    def complete(self, prompt: str, **kwargs: Any) -> CompletionResponse:
        prompt_length = len(prompt)
        tokenized = self.tokenizer(prompt)
        tokenized["input_ids"] = torch.tensor(tokenized["input_ids"]).unsqueeze(0).to("cuda")
        tokenized["attention_mask"] = torch.ones(tokenized["input_ids"].size(1)).unsqueeze(0).to("cuda")
        outputs = self.model.generate(input_ids=tokenized["input_ids"], max_new_tokens=1024, attention_mask=tokenized["attention_mask"])
        result = self.tokenizer.batch_decode(outputs, skip_special_tokens=True)[0]
        return CompletionResponse(text=result)

    @llm_callback()
    def stream_complete(self, prompt: str, **kwargs: Any) -> CompletionResponseGen:
        raise NotImplementedError()


from typing import Any, List
from InstructorEmbedding import INSTRUCTOR
from llama_index.embeddings.base import BaseEmbedding


class InstructorEmbeddings(BaseEmbedding):
    def __init__(
        self,
        instructor_model_name: str = "hkunlp/instructor-large",
        instruction: str = "Represent a document for semantic search:",
        **kwargs: Any,
    ) -> None:
        self._model = INSTRUCTOR(instructor_model_name)
        self._instruction = instruction
        super().__init__(**kwargs)

    def _get_query_embedding(self, query: str) -> List[float]:
        embeddings = self._model.encode([[self._instruction, query]])
        return embeddings[0]

    def _get_text_embedding(self, text: str) -> List[float]:
        embeddings = self._model.encode([[self._instruction, text]])
        return embeddings[0]

    def _get_text_embeddings(self, texts: List[str]) -> List[List[float]]:
        embeddings = self._model.encode([[self._instruction, text] for text in texts])
        return embeddings


class LlamaIndex(bentoml.Runnable):
    SUPPORTED_RESOURCES = ("nvidia.com/gpu",)
    SUPPORTS_CPU_MULTI_THREADING = False

    def __init__(self):
        context_window = 2048
        num_output = 1024
        documents = SimpleDirectoryReader("/docs/").load_data()
        llm = Llama2Model()
        service_context = ServiceContext.from_defaults(llm=llm, context_window=context_window, num_output=num_output, embed_model=InstructorEmbeddings(embed_batch_size=2), chunk_size=512)
        self.index = VectorStoreIndex.from_documents(documents, service_context=service_context)

    @bentoml.Runnable.method(batchable=False)
    def generate(self, input_text: str) -> bool:
        result = self.index.as_query_engine().query(input_text)
        return result


llamaindex_runner = t.cast("RunnerImpl", bentoml.Runner(LlamaIndex, name="llamaindex"))

svc = bentoml.Service("llamaindex", runners=[llamaindex_runner])


@svc.api(input=bentoml.io.Text(), output=bentoml.io.JSON())
async def infer(text: str) -> str:
    result = await llamaindex_runner.generate.async_run(text)
    return result