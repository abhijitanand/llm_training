import argparse
import torch
from torch.utils.data import DataLoader
from transformers import AutoTokenizer, AutoModel, DistilBertModel, AdamW, get_linear_schedule_with_warmup
from tqdm import tqdm
import ir_datasets
import torch.nn.functional as F

# Custom Dataset Class
class DualEncoderDataset(torch.utils.data.Dataset):
    """
    Custom dataset class for dual encoder model.
    """

    def __init__(self, tokenizer, queries, documents, labels, max_length):
        """
        Initialize the DualEncoderDataset.

        Args:
            tokenizer (AutoTokenizer): Tokenizer for tokenizing the input.
            queries (list): List of query strings.
            documents (list): List of document strings.
            labels (list): List of relevance labels.
            max_length (int): Maximum input length.
        """
        self.tokenizer = tokenizer
        self.queries = queries
        self.documents = documents
        self.labels = labels
        self.max_length = max_length

    def __len__(self):
        """
        Get the length of the dataset.

        Returns:
            int: Length of the dataset.
        """
        return len(self.queries)

    def __getitem__(self, index):
        """
        Get an item from the dataset.

        Args:
            index (int): Index of the item.

        Returns:
            dict: Dictionary containing query inputs, document inputs, and labels.
        """
        query = self.queries[index]
        document = self.documents[index]
        label = self.labels[index]

        query_inputs = self.tokenizer(query, truncation=True, padding='max_length', max_length=self.max_length, return_tensors="pt")
        doc_inputs = self.tokenizer(document, truncation=True, padding='max_length', max_length=self.max_length, return_tensors="pt")

        query_inputs = {k: v.squeeze(0) for k, v in query_inputs.items()}  # Remove batch dimension
        doc_inputs = {k: v.squeeze(0) for k, v in doc_inputs.items()}  # Remove batch dimension

        return {'query_inputs': query_inputs, 'doc_inputs': doc_inputs, 'labels': torch.tensor(label, dtype=torch.float)}

# Dual Encoder Model
class DualEncoderModel(torch.nn.Module):
    """
    Dual encoder model for ranking documents based on queries.
    """

    def __init__(self, model_name):
        """
        Initialize the DualEncoderModel.

        Args:
            model_name (str): Name or path of the pre-trained model.
        """
        super(DualEncoderModel, self).__init__()
        self.query_encoder = AutoModel.from_pretrained(model_name)
        self.doc_encoder = AutoModel.from_pretrained(model_name)

    def forward(self, query_inputs, doc_inputs):
        """
        Forward pass of the model.

        Args:
            query_inputs (dict): Dictionary containing query inputs.
            doc_inputs (dict): Dictionary containing document inputs.

        Returns:
            torch.Tensor: Scores representing the similarity between queries and documents.
        """
        if isinstance(self.query_encoder, DistilBertModel) or isinstance(self.doc_encoder, DistilBertModel):
            query_inputs.pop('token_type_ids', None)
            doc_inputs.pop('token_type_ids', None)
        
        # For DistilBertModel
        if isinstance(self.query_encoder, DistilBertModel) or isinstance(self.doc_encoder, DistilBertModel):
            query_embeds = self.query_encoder(**query_inputs).last_hidden_state[:, 0, :]  # Take the [CLS] token representation
            doc_embeds = self.doc_encoder(**doc_inputs).last_hidden_state[:, 0, :]  # Take the [CLS] token representation
        else:
            query_embeds = self.query_encoder(**query_inputs).pooler_output
            doc_embeds = self.doc_encoder(**doc_inputs).pooler_output

        scores = torch.matmul(query_embeds, doc_embeds.t())

        return scores

# Training Function
def train(model, teacher_model, tokenizer, train_dataset, val_dataset, epochs, batch_size, learning_rate, max_length, alpha, temperature):
    """
    Train the dual encoder model using distillation.

    Args:
        model (DualEncoderModel): Student model to be trained.
        teacher_model (DualEncoderModel): Teacher model for distillation.
        tokenizer (AutoTokenizer): Tokenizer for tokenizing the input.
        train_dataset (DualEncoderDataset): Training dataset.
        val_dataset (DualEncoderDataset): Validation dataset.
        epochs (int): Number of training epochs.
        batch_size (int): Training batch size.
        learning_rate (float): Learning rate.
        max_length (int): Maximum input length.
        alpha (float): Weighting factor for the ranking loss vs. distillation loss.
        temperature (float): Temperature for distillation.
    """
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    model.to(device)
    teacher_model.to(device)

    train_loader = DataLoader(train_dataset, batch_size=batch_size, shuffle=True)
    val_loader = DataLoader(val_dataset, batch_size=batch_size, shuffle=False)

    optimizer = AdamW(model.parameters(), lr=learning_rate)
    total_steps = len(train_loader) * epochs
    scheduler = get_linear_schedule_with_warmup(optimizer, num_warmup_steps=0, num_training_steps=total_steps)

    loss_fn = torch.nn.MarginRankingLoss(margin=1.0)
    distillation_loss_fn = torch.nn.KLDivLoss(reduction='batchmean')

    for epoch in range(epochs):
        model.train()
        teacher_model.eval()
        total_loss = 0
        progress_bar = tqdm(train_loader, desc=f'Epoch {epoch+1}/{epochs}')
        for batch in progress_bar:
            query_inputs = {k: v.to(device) for k, v in batch['query_inputs'].items()}
            doc_inputs = {k: v.to(device) for k, v in batch['doc_inputs'].items()}
            labels = batch['labels'].to(device)

            with torch.no_grad():
                teacher_scores = teacher_model(query_inputs, doc_inputs)

            student_scores = model(query_inputs, doc_inputs)
            positive_scores = student_scores.diag()
            negative_scores = student_scores.fill_diagonal_(float('-inf')).max(dim=1)[0]

            targets = torch.ones_like(positive_scores)
            ranking_loss = loss_fn(positive_scores, negative_scores, targets)

            teacher_scores = F.log_softmax(teacher_scores / temperature, dim=1)
            student_scores = F.log_softmax(student_scores / temperature, dim=1)
            distillation_loss = distillation_loss_fn(student_scores, teacher_scores)

            loss = alpha * ranking_loss + (1 - alpha) * distillation_loss

            optimizer.zero_grad()
            loss.backward()
            optimizer.step()
            scheduler.step()

            total_loss += loss.item()
            progress_bar.set_postfix(loss=loss.item())

        avg_train_loss = total_loss / len(train_loader)
        print(f'Epoch [{epoch+1}/{epochs}], Average Loss: {avg_train_loss:.4f}')

        evaluate(model, val_loader, device, loss_fn)

def evaluate(model, val_loader, device, loss_fn):
    """
    Evaluate the dual encoder model on the validation set.

    Args:
        model (DualEncoderModel): Model to be evaluated.
        val_loader (DataLoader): Validation data loader.
        device (torch.device): Device to run the evaluation on.
        loss_fn (torch.nn.Module): Loss function for calculating the loss.
    """
    model.eval()
    total_loss = 0

    with torch.no_grad():
        for batch in val_loader:
            query_inputs = {k: v.to(device) for k, v in batch['query_inputs'].items()}
            doc_inputs = {k: v.to(device) for k, v in batch['doc_inputs'].items()}
            labels = batch['labels'].to(device)

            scores = model(query_inputs, doc_inputs)
            positive_scores = scores.diag()
            negative_scores = scores.fill_diagonal_(float('-inf')).max(dim=1)[0]

            targets = torch.ones_like(positive_scores)
            loss = loss_fn(positive_scores, negative_scores, targets)
            total_loss += loss.item()

    avg_val_loss = total_loss / len(val_loader)
    print(f'Validation Loss: {avg_val_loss:.4f}')

def load_msmarco_data():
    """
    Load the MS MARCO dataset.

    Returns:
        tuple: Tuple containing queries, documents, and labels.
    """
    dataset = ir_datasets.load("msmarco-passage/train")
    queries = dataset.queries_iter()
    docs = dataset.docs_iter()
    qrels = dataset.qrels_iter()

    query_dict = {query.query_id: query.text for query in queries}
    doc_dict = {doc.doc_id: doc.text for doc in docs}

    queries = []
    documents = []
    labels = []

    for qrel in qrels:
        queries.append(query_dict[qrel.query_id])
        documents.append(doc_dict[qrel.doc_id])
        labels.append(qrel.relevance)

    return queries, documents, labels

def main(args):
    # Load tokenizer and models
    tokenizer = AutoTokenizer.from_pretrained(args.teacher_model_name)
    teacher_model = DualEncoderModel(args.teacher_model_name)
    student_model = DualEncoderModel(args.student_model_name)

    # Load dataset
    queries, documents, labels = load_msmarco_data()

    # For demo purposes, select a subset
    subset_size = 1000
    queries = queries[:subset_size]
    documents = documents[:subset_size]
    labels = labels[:subset_size]

    # Split into train and validation sets
    split_idx = int(0.8 * subset_size)
    train_queries = queries[:split_idx]
    train_documents = documents[:split_idx]
    train_labels = labels[:split_idx]

    val_queries = queries[split_idx:]
    val_documents = documents[split_idx:]
    val_labels = labels[split_idx:]

    # Create datasets
    train_dataset = DualEncoderDataset(tokenizer, train_queries, train_documents, train_labels, max_length=args.max_length)
    val_dataset = DualEncoderDataset(tokenizer, val_queries, val_documents, val_labels, max_length=args.max_length)

    # Fine-tune the student model using distillation
    train(student_model, teacher_model, tokenizer, train_dataset, val_dataset, epochs=args.epochs, batch_size=args.batch_size, learning_rate=args.learning_rate, max_length=args.max_length, alpha=args.alpha, temperature=args.temperature)

    # Save the fine-tuned student model
    #student_model.save_pretrained(args.save_model_path)

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    
    # Hyperparameters
    parser.add_argument("--teacher_model_name", type=str, default="bert-base-uncased", help="Teacher model name or path")
    parser.add_argument("--student_model_name", type=str, default="distilbert-base-uncased", help="Student model name or path")
    parser.add_argument("--epochs", type=int, default=3, help="Number of training epochs")
    parser.add_argument("--batch_size", type=int, default=8, help="Training batch size")
    parser.add_argument("--learning_rate", type=float, default=2e-5, help="Learning rate")
    parser.add_argument("--max_length", type=int, default=512, help="Maximum input length")
    parser.add_argument("--alpha", type=float, default=0.5, help="Weighting factor for the ranking loss vs. distillation loss")
    parser.add_argument("--temperature", type=float, default=2.0, help="Temperature for distillation")
    parser.add_argument("--save_model_path", type=str, default="./fine_tuned_student_model", help="Path to save the fine-tuned student model")

    args = parser.parse_args()
    main(args)
